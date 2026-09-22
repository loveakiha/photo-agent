"""M3c tests: schema validation, JSON extraction, cache, batch stats.

The VLM network call is mocked (the 24G-VRAM constraint means we never
want a test hitting the real llama-server). Image encoding is tested with
a real tiny JPEG written to a temp file.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from database import SCHEMA_VERSION, connect, migrate, upsert_photo
from semantic import (
    ANALYSIS_VERSION,
    PROMPT_VERSION,
    SemanticError,
    _extract_json,
    _image_to_b64,
    analyze_schema,
    analyze_batch,
    get_semantic,
    upsert_semantic,
)


@pytest.fixture
def conn():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "test.db"
        conn = connect(db)
        conn.row_factory = sqlite3.Row
        # one real photo row on disk
        img = Path(tmp) / "photo.jpg"
        from PIL import Image

        Image.new("RGB", (120, 80), (200, 30, 30)).save(str(img))
        upsert_photo(
            conn,
            rel_path="photo.jpg",
            abs_path=str(img),
            sha256="a" * 64,
            size_bytes=123,
            width=120,
            height=80,
            fmt="JPEG",
        )
        conn.commit()
        yield conn
        conn.close()


def _good_obs():
    return {
        "scene": "风景",
        "subjects": [{"name": "山", "position": "背景", "role": "主要环境"}],
        "relationships": [],
        "defects": [],
        "context": "日出光线",
        "semantic_score": 80.0,
        "score_components": {"clarity": 85, "subject": 90, "defects_avoided": 78},
        "raw_response": "{}",
    }


def test_schema_version_is_6():
    assert SCHEMA_VERSION == 6


def test_semantic_table_exists(conn):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE name='semantic_analysis'"
    ).fetchone()
    assert row is not None


def test_validate_good():
    assert analyze_schema(_good_obs()) == []


def test_validate_missing_required():
    # optional fields absent -> still valid
    problems = analyze_schema({"scene": "风景", "subjects": ["x"], "defects": [], "semantic_score": 5})
    assert problems == []
    # required fields absent -> flagged
    problems = analyze_schema({"semantic_score": 5})
    assert any("scene" in p for p in problems)
    assert any("subjects" in p for p in problems)
    assert any("defects" in p for p in problems)


def test_validate_bad_types():
    problems = analyze_schema(
        {"scene": 3, "subjects": "x", "person": None, "defects": None, "semantic_score": 999}
    )
    assert len(problems) >= 3  # scene, subjects, defects, score all bad


def test_validate_score_range():
    assert any("范围" in p for p in analyze_schema(_good_obs() | {"semantic_score": -1}))
    assert any("范围" in p for p in analyze_schema(_good_obs() | {"semantic_score": 150}))


def test_extract_json_with_fences():
    text = '```json\n{"a": 1}\n```'
    assert _extract_json(text) == {"a": 1}


def test_extract_json_no_object():
    with pytest.raises(ValueError):
        _extract_json("no json here")


def test_image_b64_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        img = Path(tmp) / "big.jpg"
        from PIL import Image

        Image.new("RGB", (3000, 2000), (10, 10, 10)).save(str(img), quality=70)
        b64 = _image_to_b64(str(img))
    assert b64.startswith("data:image/jpeg;base64,")
    payload = b64.split(",", 1)[1]
    assert len(payload) < 500_000  # downscaled to 1280 max dim


def test_upsert_and_get(conn):
    upsert_semantic(conn, 1, "model-x", PROMPT_VERSION, ANALYSIS_VERSION, _good_obs())
    got = get_semantic(conn, 1, "model-x")
    assert got is not None
    assert got["scene"] == "风景"
    assert got["subjects"] == ["山"]  # flat derived from rich objects
    assert got["semantic_score"] == 80.0
    assert isinstance(got["profile"], dict)  # full profile round-trips
    assert got["profile"]["subjects"][0]["name"] == "山"
    # overwrite under the same key
    data = _good_obs() | {"scene": "人像", "semantic_score": 55.0}
    upsert_semantic(conn, 1, "model-x", PROMPT_VERSION, ANALYSIS_VERSION, data)
    rows = conn.execute("SELECT * FROM semantic_analysis WHERE photo_id=1").fetchall()
    assert len(rows) == 1
    assert rows[0]["scene"] == "人像"
    # a different model keeps its own history row
    upsert_semantic(conn, 1, "model-y", PROMPT_VERSION, ANALYSIS_VERSION, _good_obs())
    assert len(conn.execute("SELECT * FROM semantic_analysis").fetchall()) == 2


def test_upsert_derives_person_and_rel_text(conn):
    obs = {
        "scene": "人像",
        "subjects": [
            {"name": "女性", "attributes": ["年轻"], "position": "中央",
             "role": "主要人物", "facing": "面向大海", "action": "站立"},
            {"name": "大海", "position": "背景", "role": "主要环境"},
        ],
        "relationships": ["女性 面向 大海"],
        "defects": [],
        "semantic_score": 92.0,
    }
    upsert_semantic(conn, 1, "model-x", PROMPT_VERSION, ANALYSIS_VERSION, obs)
    row = conn.execute(
        "SELECT person, rel_text, subjects FROM semantic_analysis WHERE photo_id=1"
    ).fetchone()
    assert row["person"] == "1人，站立"
    assert row["rel_text"] == "女性 面向 大海"
    assert json.loads(row["subjects"]) == ["女性", "大海"]


def test_purge_cascades_semantic(conn):
    upsert_semantic(conn, 1, "model-x", PROMPT_VERSION, ANALYSIS_VERSION, _good_obs())
    assert conn.execute("SELECT COUNT(*) FROM semantic_analysis").fetchone()[0] == 1
    # simulate purge: FK cascade from photos delete
    conn.execute("DELETE FROM photos WHERE photo_id=1")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM semantic_analysis").fetchone()[0] == 0


def _mock_backend():
    return {"base_url": "http://x", "model": "model-mock"}


def test_analyze_batch_cached_after_first(conn, monkeypatch):
    import semantic

    calls = []

    def fake_vlm(backend, image_b64, max_retries=1):
        calls.append(image_b64[:20])
        return _good_obs()

    monkeypatch.setattr(semantic, "vlm_json_call", fake_vlm)
    stats = analyze_batch(conn, _mock_backend())
    assert stats["total"] == 1
    assert stats["new"] == 1
    # second run: cache hit, no VLM call
    calls.clear()
    stats2 = analyze_batch(conn, _mock_backend())
    assert stats2["cached"] == 1
    assert calls == []


def test_analyze_batch_unreliable_on_error(conn, monkeypatch):
    import semantic

    def fake_vlm(backend, image_b64, max_retries=1):
        raise SemanticError("bad json twice")

    monkeypatch.setattr(semantic, "vlm_json_call", fake_vlm)
    stats = analyze_batch(conn, _mock_backend())
    assert stats["unreliable"] == 1
    row = conn.execute(
        "SELECT raw_response FROM semantic_analysis WHERE photo_id=1"
    ).fetchone()
    assert row is not None and "semantic_unreliable" in row["raw_response"]


def test_analyze_batch_unreadable_file(conn, monkeypatch):
    import semantic

    # break the photo's path
    conn.execute("UPDATE photos SET abs_path='/nonexistent/file.jpg' WHERE photo_id=1")
    conn.commit()
    stats = analyze_batch(conn, _mock_backend())
    assert stats["unreliable"] == 1
