"""M3b Decision Policy unit tests.

Covers the spec's required scenarios:
  exact duplicate, near duplicate, quality tie, resolution tie,
  metadata missing, unreadable file, format tie, deterministic tie-break.
"""
from __future__ import annotations

import pytest

from database import connect
from policy import (
    POLICY_DISCARD,
    POLICY_KEEP,
    USER_DISCARD,
    USER_KEEP,
    run_policy,
    user_confirm,
)


def _photo(conn, pid, rel, size=1000, width=4000, height=3000, fmt="JPEG",
           taken_at: str | None = "2026-01-01T00:00:00",
           camera: str | None = "TestCam"):
    conn.execute(
        """INSERT INTO photos
           (photo_id, rel_path, abs_path, sha256, size_bytes, width, height,
            format, taken_at, camera_make)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (pid, rel, f"/tmp/{rel}", f"sha{pid}", size, width, height,
         fmt, taken_at, camera),
    )


def _quality(conn, pid, sharp=0.9, expo=0.9, noise=0.9):
    conn.execute(
        """INSERT INTO quality
           (photo_id, sharpness, exposure, noise, algorithm_version)
           VALUES (?,?,?,?,?)""",
        (pid, sharp, expo, noise, "test"),
    )


def _group(conn, gid, kind, member_ids):
    rep = member_ids[0]
    conn.execute(
        "INSERT INTO groups (group_id, kind, rep_photo_id, size) VALUES (?,?,?,?)",
        (gid, kind, rep, len(member_ids)),
    )
    for mid in member_ids:
        conn.execute(
            "INSERT INTO group_members (group_id, photo_id) VALUES (?,?)",
            (gid, mid),
        )


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "policy.db"
    c = connect(db)
    yield c
    c.close()


def _decisions(conn) -> dict:
    return {
        row[0]: (row[1], row[2], row[3], row[4])  # (status, final_score, reason, source)
        for row in conn.execute(
            "SELECT photo_id, status, final_score, reason, source "
            "FROM decisions"
        )
    }


# --- exact duplicate: metadata wins ----------------------------------------

def test_exact_duplicate_metadata_wins(conn):
    _photo(conn, 1, "a.jpg", size=1000, taken_at="2026-01-01T00:00:00")
    _photo(conn, 2, "b.jpg", size=1000, taken_at=None)  # less metadata, same size
    _quality(conn, 1)
    _quality(conn, 2)
    _group(conn, 1, "exact", [1, 2])

    run_policy(conn)
    d = _decisions(conn)
    assert d[1][0] == POLICY_KEEP
    assert d[2][0] == POLICY_DISCARD
    # reason must be concrete: cite the representative it lost to
    assert "loses to" in d[2][2]
    assert d[1][3] == "policy"


# --- near duplicate: quality wins ------------------------------------------

def test_near_duplicate_quality_wins(conn):
    _photo(conn, 1, "a.jpg", size=1000)
    _photo(conn, 2, "b.jpg", size=1000)
    _quality(conn, 1, sharp=0.9, expo=0.9, noise=0.9)
    _quality(conn, 2, sharp=0.3, expo=0.9, noise=0.9)  # worse
    _group(conn, 1, "near", [1, 2])

    run_policy(conn)
    d = _decisions(conn)
    assert d[1][0] == POLICY_KEEP
    assert d[2][0] == POLICY_DISCARD
    assert "quality" in d[2][2]


# --- quality tie -> resolution breaks --------------------------------------

def test_quality_tie_resolution_breaks(conn):
    _photo(conn, 1, "a.jpg", width=4000, height=3000)
    _photo(conn, 2, "b.jpg", width=6000, height=4000)  # bigger
    _quality(conn, 1)
    _quality(conn, 2)
    _group(conn, 1, "near", [1, 2])

    run_policy(conn)
    d = _decisions(conn)
    assert d[2][0] == POLICY_KEEP  # higher resolution wins the tie
    assert d[1][0] == POLICY_DISCARD
    assert "resolution" in d[1][2]


# --- quality + resolution tie -> file size (format proxy) ------------------

def test_full_tie_file_size_breaks(conn):
    _photo(conn, 1, "a.jpg", size=1000, width=4000, height=3000)
    _photo(conn, 2, "b.jpg", size=900, width=4000, height=3000)  # smaller
    _quality(conn, 1)
    _quality(conn, 2)
    _group(conn, 1, "near", [1, 2])

    run_policy(conn)
    d = _decisions(conn)
    assert d[1][0] == POLICY_KEEP
    assert d[2][0] == POLICY_DISCARD


# --- metadata missing on one side ------------------------------------------

def test_metadata_missing_lower_completeness(conn):
    _photo(conn, 1, "a.jpg", taken_at="2026-01-01T00:00:00", camera="Cam")
    _photo(conn, 2, "b.jpg", taken_at=None, camera=None)  # missing
    _quality(conn, 1)
    _quality(conn, 2)
    _group(conn, 1, "near", [1, 2])

    run_policy(conn)
    d = _decisions(conn)
    assert d[1][0] == POLICY_KEEP
    assert d[2][0] == POLICY_DISCARD


# --- unreadable file (no dimensions) loses ---------------------------------

def test_unreadable_file_loses(conn):
    # width/height NULL => not readable
    _photo(conn, 1, "a.jpg", width=4000, height=3000)
    conn.execute(
        """INSERT INTO photos
           (photo_id, rel_path, abs_path, sha256, size_bytes, format)
           VALUES (2, 'b.jpg', '/tmp/b.jpg', 'sha2', 1000, 'JPEG')"""
    )
    _quality(conn, 1)
    _quality(conn, 2)
    _group(conn, 1, "near", [1, 2])

    run_policy(conn)
    d = _decisions(conn)
    assert d[1][0] == POLICY_KEEP
    assert d[2][0] == POLICY_DISCARD
    assert "readable" in d[2][2] or "loses" in d[2][2]


# --- deterministic tie-break (identical everything -> stable by id/path) --

def test_deterministic_tie_break_stable(conn):
    for pid in (1, 2):
        _photo(conn, pid, f"p{pid}.jpg", size=1000, width=4000, height=3000)
        _quality(conn, pid)
    _group(conn, 1, "near", [1, 2])

    run_policy(conn)
    first = _decisions(conn)
    # rerun on the same state must produce identical outcome
    run_policy(conn)
    second = _decisions(conn)
    assert first == second
    # lowest photo_id / stable path wins when everything ties
    statuses = {pid: s for pid, (s, *_rest) in first.items()}
    assert statuses[1] == POLICY_KEEP
    assert statuses[2] == POLICY_DISCARD


# --- user decision is never overwritten by policy --------------------------

def test_user_decision_protected_from_policy(conn):
    _photo(conn, 1, "a.jpg")
    _photo(conn, 2, "b.jpg", size=900)
    _quality(conn, 1)
    _quality(conn, 2)
    _group(conn, 1, "near", [1, 2])

    # user confirms 2 (the smaller one) as representative
    user_confirm(conn, group_id=1, keep_photo_id=2)
    d = _decisions(conn)
    assert d[2][0] == USER_KEEP and d[2][3] == "user"

    # policy runs: must NOT clobber the user row, and must not re-recommend 2
    run_policy(conn)
    d = _decisions(conn)
    assert d[2][0] == USER_KEEP and d[2][3] == "user"
    # 1 should be discard (policy) but keep 2 stays user-kept
    assert d[1][0] in (USER_DISCARD, POLICY_DISCARD)


# --- user_confirm ignores others when asked ---------------------------------

def test_user_confirm_ignore_others(conn):
    _photo(conn, 1, "a.jpg")
    _photo(conn, 2, "b.jpg")
    _quality(conn, 1)
    _quality(conn, 2)
    _group(conn, 1, "near", [1, 2])

    result = user_confirm(conn, group_id=1, keep_photo_id=1, others=("IGNORE",))
    assert result["keep"] == 1
    d = _decisions(conn)
    assert d[1][0] == USER_KEEP
    # 2 left without a discard row
    assert 2 not in d


# --- reason carries structured rule version --------------------------------

def test_reason_has_rule_version(conn):
    _photo(conn, 1, "a.jpg")
    _photo(conn, 2, "b.jpg", size=900)
    _quality(conn, 1)
    _quality(conn, 2)
    _group(conn, 1, "near", [1, 2])
    run_policy(conn)
    d = _decisions(conn)
    assert "m3b-v1" in d[1][2]
    assert "m3b-v1" in d[2][2]
