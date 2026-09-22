"""M3.1 Taste Profile tests: profile aggregation, bounded bias, taste-aware
ranking with near-dup dedup and user-winner pinning. All zero-VLM.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from database import SCHEMA_VERSION, connect
from taste import (
    BIAS_MAX,
    build_taste_profile,
    profile_summary,
    rank_top_n,
    taste_bias,
)

PROMPT_VERSION = "m3c-v2"
ANALYSIS_VERSION = "m3c-v2"


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "taste.db"
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
        (pid, "model-x", PROMPT_VERSION, ANALYSIS_VERSION, scene,
         json.dumps(["x"], ensure_ascii=False), person,
         json.dumps([]), score),
    )


def _quality(c, pid, expo):
    c.execute(
        """INSERT INTO quality (photo_id, sharpness, exposure, noise,
            algorithm_version) VALUES (?,?,?,?,?)""",
        (pid, 0.9, expo, 0.9, "test"),
    )


def _user_decision(c, pid, status):
    c.execute(
        """INSERT INTO decisions (photo_id, status, source, rule_version, updated_at)
           VALUES (?,?,?,?,datetime('now'))""",
        (pid, status, "user", "m3b-v1"),
    )


def test_empty_profile_no_decisions(conn):
    p = build_taste_profile(conn)
    assert p["n_samples"] == 0
    assert p["person_share"] is None
    assert taste_bias(p, "风景", 0.5, None) == 0.0


def test_profile_counts_kept_discarded(conn):
    for pid in (1, 2, 3):
        _photo(conn, pid, f"p{pid}.jpg")
        _semantic(conn, pid, "风景", 90.0)
    _user_decision(conn, 1, "KEEP")
    _user_decision(conn, 2, "KEEP")
    _user_decision(conn, 3, "DISCARD")
    p = build_taste_profile(conn)
    assert p["n_kept"] == 2
    assert p["n_discarded"] == 1
    assert p["n_samples"] == 3


def test_scene_affinity_reflects_kept_share(conn):
    # 4 landscape kept, 1 portrait kept, baseline mixed.
    for pid in range(1, 5):
        _photo(conn, pid, f"p{pid}.jpg")
        _semantic(conn, pid, "风景", 90.0)
        _user_decision(conn, pid, "KEEP")
    _photo(conn, 5, "p5.jpg")
    _semantic(conn, 5, "人像", 90.0)
    _user_decision(conn, 5, "KEEP")
    # baseline: add portrait photos the user did NOT keep
    for pid in (6, 7, 8):
        _photo(conn, pid, f"p{pid}.jpg")
        _semantic(conn, pid, "人像", 70.0)
    p = build_taste_profile(conn)
    # user over-indexes 风景 relative to its baseline share
    assert p["scene_affinity"]["风景"] > 0
    # user under-indexes 人像 (kept only 1 of 4 portrait photos)
    assert p["scene_affinity"]["人像"] < 0


def test_bias_bounded(conn):
    p = build_taste_profile(conn)
    # fabricate an extreme profile: huge affinity lift
    p["n_samples"] = 1
    p["scene_affinity"]["风景"] = 5.0
    p["exposure_lift"] = 1.0
    b = taste_bias(p, "风景", 1.0, None)
    assert abs(b) <= BIAS_MAX
    assert b == BIAS_MAX


def test_bias_zero_with_no_samples(conn):
    p = build_taste_profile(conn)
    p["scene_affinity"]["风景"] = 1.0  # but no samples -> must stay 0
    assert p["n_samples"] == 0
    assert taste_bias(p, "风景", 0.5, None) == 0.0


def test_rank_top_n_pure_semantic(conn):
    # no user decisions -> bias 0, ranking == semantic_score desc
    for pid, score in [(1, 92.0), (2, 90.0), (3, 88.0)]:
        _photo(conn, pid, f"p{pid}.jpg")
        _semantic(conn, pid, "风景", score)
    rows = rank_top_n(conn, "风景", n=3, profile=None)
    assert [r["photo_id"] for r in rows] == [1, 2, 3]
    assert rows[0]["bias"] == 0.0


def test_rank_dedup_one_slot_per_group(conn):
    # two near-dup photos in the same group -> only one occupies a slot
    for pid, score in [(1, 92.0), (2, 91.9)]:
        _photo(conn, pid, f"p{pid}.jpg")
        _semantic(conn, pid, "风景", score)
    _photo(conn, 3, "p3.jpg")
    _semantic(conn, 3, "风景", 91.0)
    conn.execute(
        "INSERT INTO groups (group_id, kind, rep_photo_id, size) VALUES (1,'near',1,2)"
    )
    conn.execute("INSERT INTO group_members (group_id, photo_id) VALUES (1,1)")
    conn.execute("INSERT INTO group_members (group_id, photo_id) VALUES (1,2)")
    rows = rank_top_n(conn, "风景", n=3, profile=None)
    # 1 and 2 same group -> only #1 (higher) appears; #3 fills second slot
    ids = [r["photo_id"] for r in rows]
    assert 1 in ids and 2 not in ids and 3 in ids
    assert len(ids) == 2


def test_rank_pins_user_winner(conn):
    # user confirmed #2 as group winner; #1 scored higher but must lose
    for pid, score in [(1, 92.0), (2, 91.5)]:
        _photo(conn, pid, f"p{pid}.jpg")
        _semantic(conn, pid, "风景", score)
    _photo(conn, 3, "p3.jpg")
    _semantic(conn, 3, "风景", 91.0)
    conn.execute(
        "INSERT INTO groups (group_id, kind, rep_photo_id, size) VALUES (1,'near',2,2)"
    )
    conn.execute("INSERT INTO group_members (group_id, photo_id) VALUES (1,1)")
    conn.execute("INSERT INTO group_members (group_id, photo_id) VALUES (1,2)")
    _user_decision(conn, 2, "KEEP")
    rows = rank_top_n(conn, "风景", n=3, profile=None)
    ids = [r["photo_id"] for r in rows]
    # #2 (user winner) represents the group; #1 skipped
    assert 2 in ids and 1 not in ids


def test_profile_summary_empty(conn):
    assert "暂无用户决定" in profile_summary(build_taste_profile(conn))


def test_schema_version_still_6(conn):
    assert SCHEMA_VERSION == 6
