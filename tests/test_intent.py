"""M3.2 Intent tests: preset matching, free-form parsing, candidate retrieval.
Zero VLM.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from database import connect
from intent import (
    parse_intent,
    retrieve_candidates,
    intent_summary,
)


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "intent.db"
    c = connect(db)
    yield c
    c.close()


def _photo(c, pid, rel, taken_at="2026-01-01T00:00:00"):
    c.execute(
        """INSERT INTO photos (photo_id, rel_path, abs_path, sha256, size_bytes,
            width, height, format, taken_at, camera_make)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (pid, rel, f"/tmp/{rel}", f"sha{pid}", 1000, 4000, 3000, "JPEG",
         taken_at, "Cam"),
    )


def _semantic(c, pid, scene, score, person=None):
    c.execute(
        """INSERT INTO semantic_analysis
           (photo_id, model, prompt_version, analysis_version, scene,
            subjects, person, defects, semantic_score)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (pid, "model-x", "m3c-v2", "m3c-v2", scene,
         json.dumps(["x"], ensure_ascii=False),
         json.dumps([person], ensure_ascii=False) if person else None,
         json.dumps([]), score),
    )


def test_empty_intent():
    p = parse_intent("")
    assert p["intent_type"] == "empty"
    assert p["scene"] is None
    assert p["time_scope"] == "all"


def test_freeform_pronoun_does_not_require_person():
    p = parse_intent("我想发海边照片")
    assert p["scene"] == "风景"
    assert p["person"] is None


def test_explicit_year_is_parsed():
    p = parse_intent("2025年拍的旅行照片")
    assert p["year"] == 2025


def test_relative_year_is_parsed():
    from datetime import datetime

    assert parse_intent("去年拍的照片")["year"] == datetime.now().year - 1

def test_preset_just_back_from_trip():
    p = parse_intent("刚旅行回来，想发个朋友圈")
    assert p["intent_type"] == "just_back_from_trip"
    assert p["scene"] == "风景"
    assert p["time_scope"] == "recent"


def test_preset_dont_be_overdone():
    p = parse_intent("刚旅行回来，想发个朋友圈，看起来这趟旅行挺丰富的，但不要太刻意。")
    assert p["intent_type"] == "just_back_from_trip"
    assert p["exclude_overdone"] is True


def test_preset_out_with_friends():
    p = parse_intent("和朋友出去玩")
    assert p["intent_type"] == "out_with_friends"
    assert p["person"] is True
    assert p["person_required"] is True


def test_preset_pick_any():
    p = parse_intent("随便帮我选")
    assert p["intent_type"] == "pick_any"
    assert p["time_scope"] == "all"
    assert p["scene"] is None


def test_preset_want_rich_life():
    p = parse_intent("想显得最近生活很丰富")
    assert p["intent_type"] == "want_rich_life"
    assert p["tone"] == "refined"
    assert p["time_scope"] == "recent"


def test_freeform_scene_person_tone():
    p = parse_intent("想发个海边的照片，有我和朋友，氛围感强一点")
    assert p["intent_type"] == "free"
    assert p["scene"] == "风景"
    assert p["person"] is True
    assert p["tone"] == "atmosphere"


def test_freeform_no_face():
    p = parse_intent("不要露脸，只要风景")
    assert p["person"] is False
    assert p["scene"] == "风景"


def test_freeform_food_recent():
    p = parse_intent("最近吃的寿司")
    assert p["scene"] == "食物"
    assert p["time_scope"] == "recent"


def test_freeform_tone_refined():
    # "今天心情不错" matches feeling_good_today (preset tone=natural), but the
    # user also explicitly says "精致" -> free-form tone overrides the preset.
    p = parse_intent("今天心情不错，想要精致点的照片")
    assert p["intent_type"] == "feeling_good_today"
    assert p["tone"] == "refined"


def test_freeform_time_today():
    p = parse_intent("今天拍的花")
    assert p["time_scope"] == "today"
    assert p["scene"] == "静物"


def test_freeform_overdone_negative_in_free():
    p = parse_intent("想发点风景，不要太刻意" + "，自然一点")
    assert p["exclude_overdone"] is True
    assert p["tone"] == "natural"


def test_retrieve_candidates_no_filter(conn):
    for pid, scene, score in [(1, "风景", 92.0), (2, "人像", 90.0),
                              (3, "风景", 88.0)]:
        _photo(conn, pid, f"p{pid}.jpg")
        _semantic(conn, pid, scene, score)
    rows = retrieve_candidates(conn, parse_intent(""), limit=10)
    assert len(rows) == 3
    assert rows[0]["photo_id"] == 1  # highest score first


def test_retrieve_candidates_scene_filter(conn):
    for pid, scene, score in [(1, "风景", 92.0), (2, "人像", 90.0),
                              (3, "风景", 88.0)]:
        _photo(conn, pid, f"p{pid}.jpg")
        _semantic(conn, pid, scene, score)
    rows = retrieve_candidates(conn, parse_intent("风景"), limit=10)
    assert len(rows) == 2
    assert all(r["scene"] == "风景" for r in rows)


def test_retrieve_candidates_person_required(conn):
    for pid, scene, score, person in [
        (1, "人像", 92.0, "我"),
        (2, "风景", 90.0, None),
        (3, "人像", 88.0, "朋友"),
    ]:
        _photo(conn, pid, f"p{pid}.jpg")
        _semantic(conn, pid, scene, score, person=person)
    rows = retrieve_candidates(conn, parse_intent("有朋友合影"), limit=10)
    assert len(rows) == 2
    assert {r["photo_id"] for r in rows} == {1, 3}


def test_retrieve_candidates_exclude_person(conn):
    for pid, scene, score, person in [
        (1, "风景", 92.0, None),
        (2, "人像", 90.0, "我"),
        (3, "风景", 88.0, "朋友"),
    ]:
        _photo(conn, pid, f"p{pid}.jpg")
        _semantic(conn, pid, scene, score, person=person)
    rows = retrieve_candidates(conn, parse_intent("不要露脸"), limit=10)
    # only the no-person photo should remain
    assert len(rows) == 1
    assert rows[0]["photo_id"] == 1


def test_retrieve_candidates_year_filter(conn):
    _photo(conn, 1, "old.jpg", "2025-06-01T12:00:00")
    _photo(conn, 2, "new.jpg", "2026-06-01T12:00:00")
    _semantic(conn, 1, "风景", 90.0)
    _semantic(conn, 2, "风景", 91.0)
    rows = retrieve_candidates(conn, parse_intent("2025年的风景"), limit=10)
    assert [r["photo_id"] for r in rows] == [1]

def test_intent_summary():
    p = parse_intent("刚旅行回来，想发个朋友圈，不要太刻意")
    s = intent_summary(p)
    assert "just_back_from_trip" in s
    assert "风景" in s
    assert "不要" in s or "刻意" in s
