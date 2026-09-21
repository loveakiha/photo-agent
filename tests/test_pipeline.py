"""M3.3 pipeline tests: end-to-end funnel, dedup, taste bias, user-winner
pinning, quality floor. Zero VLM.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from database import connect
from pipeline import select_photos, pipeline_summary
from intent import parse_intent


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "pipeline.db"
    c = connect(db)
    yield c
    c.close()


def _photo(c, pid, rel):
    c.execute(
        """INSERT INTO photos (photo_id, rel_path, abs_path, sha256, size_bytes,
            width, height, format, taken_at, camera_make)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (pid, rel, f"/tmp/{rel}", f"sha{pid}", 1000, 4000, 3000, "JPEG",
         "2026-01-01T00:00:00", "Cam"),
    )


def _semantic(c, pid, scene, score, person=None):
    c.execute(
        """INSERT INTO semantic_analysis
           (photo_id, model, prompt_version, analysis_version, scene,
            subjects, person, defects, semantic_score)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (pid, "model-x", "m3c-v1", "m3c-v1", scene,
         json.dumps(["x"], ensure_ascii=False),
         json.dumps([person], ensure_ascii=False) if person else None,
         json.dumps([]), score),
    )


def _user_decision(c, pid, status):
    c.execute(
        """INSERT INTO decisions (photo_id, status, source, rule_version, updated_at)
           VALUES (?,?,?,?,datetime('now'))""",
        (pid, status, "user", "m3b-v1"),
    )


def test_pipeline_empty_db(conn):
    result = select_photos(conn, "随便帮我选", n=5)
    assert result["stages"]["final"] == 0
    assert result["candidates"] == []
    assert result["intent"]["intent_type"] == "pick_any"


def test_pipeline_scene_intent_funnel(conn):
    # 3 landscape, 1 portrait.
    for pid, scene, score in [(1, "风景", 92.0), (2, "风景", 90.0),
                              (3, "风景", 88.0), (4, "人像", 91.0)]:
        _photo(conn, pid, f"p{pid}.jpg")
        _semantic(conn, pid, scene, score)
    result = select_photos(conn, "风景", n=5)
    assert result["stages"]["recalled"] == 3
    assert result["stages"]["final"] == 3
    assert all(c["scene"] == "风景" for c in result["candidates"])
    # ordered by final_score desc
    assert [c["photo_id"] for c in result["candidates"]] == [1, 2, 3]


def test_pipeline_quality_floor(conn):
    for pid, score in [(1, 92.0), (2, 85.0), (3, 70.0)]:
        _photo(conn, pid, f"p{pid}.jpg")
        _semantic(conn, pid, "风景", score)
    result = select_photos(conn, "风景", n=5, semantic_score_floor=80.0)
    assert result["stages"]["after_quality"] == 2  # 70 filtered out
    assert {c["photo_id"] for c in result["candidates"]} == {1, 2}


def test_pipeline_dedup_near_dup(conn):
    # 1 & 2 in same near-dup group; 3 separate.
    for pid, score in [(1, 92.0), (2, 91.5), (3, 91.0)]:
        _photo(conn, pid, f"p{pid}.jpg")
        _semantic(conn, pid, "风景", score)
    conn.execute(
        "INSERT INTO groups (group_id, kind, rep_photo_id, size) VALUES (1,'near',1,2)"
    )
    conn.execute("INSERT INTO group_members (group_id, photo_id) VALUES (1,1)")
    conn.execute("INSERT INTO group_members (group_id, photo_id) VALUES (1,2)")
    result = select_photos(conn, "风景", n=5)
    ids = [c["photo_id"] for c in result["candidates"]]
    assert 1 in ids and 2 not in ids  # dedup: only #1 from the group
    assert 3 in ids
    assert len(ids) == 2


def test_pipeline_pins_user_winner(conn):
    # user confirmed #2; #1 higher but must lose the group slot.
    for pid, score in [(1, 92.0), (2, 91.5), (3, 91.0)]:
        _photo(conn, pid, f"p{pid}.jpg")
        _semantic(conn, pid, "风景", score)
    conn.execute(
        "INSERT INTO groups (group_id, kind, rep_photo_id, size) VALUES (1,'near',2,2)"
    )
    conn.execute("INSERT INTO group_members (group_id, photo_id) VALUES (1,1)")
    conn.execute("INSERT INTO group_members (group_id, photo_id) VALUES (1,2)")
    _user_decision(conn, 2, "KEEP")
    result = select_photos(conn, "风景", n=5)
    ids = [c["photo_id"] for c in result["candidates"]]
    assert 2 in ids and 1 not in ids


def test_pipeline_profile_reflects_user_decisions(conn):
    for pid in (1, 2, 3):
        _photo(conn, pid, f"p{pid}.jpg")
        _semantic(conn, pid, "风景", 90.0)
    _user_decision(conn, 1, "KEEP")
    _user_decision(conn, 2, "DISCARD")
    result = select_photos(conn, "风景", n=5)
    assert result["profile"]["n_kept"] == 1
    assert result["profile"]["n_discarded"] == 1


def test_pipeline_summary_format(conn):
    result = select_photos(conn, "随便帮我选", n=5)
    s = pipeline_summary(result)
    assert "漏斗" in s
    assert "画像" in s


def test_pipeline_person_intent(conn):
    for pid, scene, score, person in [
        (1, "人像", 92.0, "我"), (2, "风景", 90.0, None), (3, "人像", 88.0, "朋友"),
    ]:
        _photo(conn, pid, f"p{pid}.jpg")
        _semantic(conn, pid, scene, score, person=person)
    result = select_photos(conn, "有朋友合影", n=5)
    # only person-present frames
    assert {c["photo_id"] for c in result["candidates"]} == {1, 3}
