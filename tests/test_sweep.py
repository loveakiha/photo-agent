"""M1 sweep: read-only parameter search.

Covers: (1) the sweep produces the same pair/group counts as the official
near pass for each combo, (2) it never writes to the database (the DB's
official near groups stay untouched), (3) ``threshold_groups`` returns
in-memory member lists without persisting, and (4) the CLI threshold helpers
(config default resolution, combo parsing).
"""
from __future__ import annotations

from cli import _parse_combo, near_thresholds, resolve_thresholds
from database import connect, upsert_photo, upsert_photo_hashes
from hashes import HASH_VERSION
from near import build_near_groups, threshold_groups
from sweep import run_sweep


def _insert_hashed(conn, root, rel_path, sha, phash, dhash):
    abs_path = root / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(b"")
    photo_id, _ = upsert_photo(
        conn, rel_path=rel_path, abs_path=str(abs_path), sha256=sha
    )
    upsert_photo_hashes(conn, photo_id, phash, dhash, HASH_VERSION)
    return photo_id


def _seed(tmp_path):
    """A small live set: a 3-photo near cluster (A~B, B~C) plus one
    isolated far photo D."""
    db = tmp_path / "photo.db"
    files = tmp_path / "files"
    conn = connect(db)
    A = "0000000000000000"
    B = "0000000000000001"  # 1 bit from A
    C = "00000000000001ff"  # 8 bits from B, 9 from A
    D = "ffffffffffffffff"  # far from everything
    _insert_hashed(conn, files, "a.jpg", "a" * 64, A, A)
    _insert_hashed(conn, files, "b.jpg", "b" * 64, B, A)
    _insert_hashed(conn, files, "c.jpg", "c" * 64, C, A)
    _insert_hashed(conn, files, "d.jpg", "d" * 64, D, D)
    return conn


def test_sweep_counts_match_near_pass(tmp_path):
    conn = _seed(tmp_path)
    try:
        out = run_sweep(conn, phash_grid=(8,), dhash_grid=(12,))
        assert out["photos"] == 4
        row = out["results"][0]
        # Same combo as `near --phash 8 --dhash 12`.
        official = build_near_groups(conn, phash_threshold=8, dhash_threshold=12)
        assert row["pairs"] == official["near_pairs"] == 2
        assert row["groups"] == official["near_groups"] == 1
        assert row["members"] == 3
        assert abs(row["coverage"] - 3 / 4) < 1e-9
    finally:
        conn.close()


def test_sweep_is_read_only(tmp_path):
    """Running the sweep must not create/modify any near groups in the DB."""
    conn = _seed(tmp_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM groups WHERE kind='near'").fetchone()[0] == 0
        run_sweep(conn, phash_grid=(4, 8), dhash_grid=(12,))
        assert conn.execute("SELECT COUNT(*) FROM groups WHERE kind='near'").fetchone()[0] == 0
    finally:
        conn.close()


def test_threshold_groups_returns_members_without_writing(tmp_path):
    conn = _seed(tmp_path)
    try:
        groups = threshold_groups(conn, 8, 12)
        assert len(groups) == 1
        names = [m["rel_path"] for m in groups[0]]
        assert names == ["a.jpg", "b.jpg", "c.jpg"]
        assert conn.execute("SELECT COUNT(*) FROM groups WHERE kind='near'").fetchone()[0] == 0
    finally:
        conn.close()


def test_sweep_stricter_threshold_splits_cluster(tmp_path):
    conn = _seed(tmp_path)
    try:
        out = run_sweep(conn, phash_grid=(1,), dhash_grid=(12,))
        row = out["results"][0]
        assert row["pairs"] == 1
        assert row["groups"] == 1
        assert row["members"] == 2
    finally:
        conn.close()


# --- CLI threshold helpers --------------------------------------------------


def test_near_thresholds_defaults_and_override():
    # No config -> code defaults.
    assert near_thresholds({}) == (8, 12)
    # Full section.
    assert near_thresholds({"near": {"phash": 10, "dhash": 14}}) == (10, 14)
    # Partial section: missing key falls back to the code default.
    assert near_thresholds({"near": {"phash": 6}}) == (6, 12)
    # resolve_thresholds: CLI value > config > code default.
    assert resolve_thresholds({}, 5) == (5, 12)
    assert resolve_thresholds({}, None, 13) == (8, 13)
    assert resolve_thresholds({"near": {"dhash": 14}}, None, 13) == (8, 13)
    assert resolve_thresholds({"near": {"phash": 6, "dhash": 14}}, 5, 13) == (5, 13)


def test_parse_combo():
    assert _parse_combo("phash8_dhash12") == (8, 12)
    assert _parse_combo("PHASH4_DHASH16") == (4, 16)  # case-insensitive
    assert _parse_combo("phash12_dhash8") == (12, 8)
    # Malformed.
    assert _parse_combo("garbage") is None
    assert _parse_combo("phash8_dhash12x") is None
    assert _parse_combo("phash8_12") is None
    assert _parse_combo("dhash12_phash8") is None
