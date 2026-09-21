from __future__ import annotations

import random

from PIL import Image

from database import connect, upsert_photo, upsert_photo_hashes
from hashes import HASH_VERSION
from near import build_near_groups
from scanner import scan
from similarity import hamming_distance


def _make_noisy(path, seed: int, quality: int):
    """A pseudo-random-noise image, re-encoded at the given JPEG quality."""
    rng = random.Random(seed)
    size = 96
    img = Image.new("RGB", (size, size))
    img.putdata([tuple(rng.randrange(256) for _ in range(3)) for _ in range(size * size)])
    img.save(path, "JPEG", quality=quality)


def _checkerboard(path, quality=90):
    size = 96
    cell = 12
    img = Image.new("RGB", (size, size))
    img.putdata(
        [
            (255, 255, 255) if ((x // cell + y // cell) % 2 == 0) else (0, 0, 0)
            for y in range(size)
            for x in range(size)
        ]
    )
    img.save(path, "JPEG", quality=quality)


def _diagonal_stripe(path, quality=90):
    size = 96
    img = Image.new("RGB", (size, size))
    img.putdata(
        [(255, 255, 255) if ((x + y) // 12) % 2 == 0 else (0, 0, 0)
         for y in range(size)
         for x in range(size)]
    )
    img.save(path, "JPEG", quality=quality)


CFG = {
    "input": {"extensions": ["jpg"], "ignore_dirs": []},
    "thumbnail": {"size": 32},
}


def test_near_group_from_recompression(tmp_path):
    photos = tmp_path / "photos"
    photos.mkdir()
    # Same visual content, two encodings -> should be a near pair.
    _make_noisy(photos / "shot-q90.jpg", seed=42, quality=90)
    _make_noisy(photos / "shot-q50.jpg", seed=42, quality=50)
    # Distinct structure -> must not join the group.
    _checkerboard(photos / "checker.jpg")

    db = tmp_path / "photo.db"
    thumbs = tmp_path / "work" / "thumbs"
    conn = connect(db)
    try:
        stats = scan(photos, CFG, conn, thumbs)
        assert stats["hash_ok"] == 3

        result = build_near_groups(conn)
        assert result["near_groups"] == 1
        assert result["near_pairs"] == 1

        group = conn.execute(
            "SELECT * FROM groups WHERE kind='near'"
        ).fetchone()
        assert group is not None
        assert group["size"] == 2
        members = {
            row["rel_path"]
            for row in conn.execute(
                """
                SELECT p.rel_path FROM group_members gm
                JOIN photos p ON p.photo_id = gm.photo_id
                WHERE gm.group_id = ?
                """,
                (group["group_id"],),
            )
        }
        assert members == {"shot-q90.jpg", "shot-q50.jpg"}

        # M1.0 writes candidates only: no decisions rows.
        assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0
    finally:
        conn.close()


def test_no_near_groups_for_distinct_photos(tmp_path):
    photos = tmp_path / "photos"
    photos.mkdir()
    # Three structurally distinct images: noise, checkerboard, diagonal
    # stripes. None should fall within threshold of another.
    _make_noisy(photos / "a.jpg", seed=1, quality=90)
    _checkerboard(photos / "b.jpg")
    _diagonal_stripe(photos / "c.jpg")

    db = tmp_path / "photo.db"
    thumbs = tmp_path / "work" / "thumbs"
    conn = connect(db)
    try:
        scan(photos, CFG, conn, thumbs)
        result = build_near_groups(conn)
        assert result["near_groups"] == 0
        assert result["near_pairs"] == 0
        assert result["near_members"] == 0
        assert conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0] == 0
    finally:
        conn.close()


def test_near_groups_are_idempotent(tmp_path):
    photos = tmp_path / "photos"
    photos.mkdir()
    _make_noisy(photos / "x-q90.jpg", seed=7, quality=90)
    _make_noisy(photos / "x-q50.jpg", seed=7, quality=50)

    db = tmp_path / "photo.db"
    thumbs = tmp_path / "work" / "thumbs"
    conn = connect(db)
    try:
        scan(photos, CFG, conn, thumbs)
        first = build_near_groups(conn)
        second = build_near_groups(conn)
        assert first == second
        assert conn.execute(
            "SELECT COUNT(*) FROM groups WHERE kind='near'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Deterministic tests using hand-crafted hashes (no image decoding involved)
# ---------------------------------------------------------------------------


def _insert_hashed(conn, root, rel_path: str, sha: str, phash: str, dhash: str):
    """Insert a photo + hash row where the file actually exists on disk
    (near grouping now requires ``abs_path`` to be a live file)."""
    abs_path = root / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(b"")
    photo_id, _ = upsert_photo(
        conn, rel_path=rel_path, abs_path=str(abs_path), sha256=sha
    )
    upsert_photo_hashes(
        conn, photo_id, phash, dhash, HASH_VERSION
    )
    return photo_id


def test_chain_clustering_is_connected_component(tmp_path):
    """A~B and B~C are near, but A~C exceeds the pHash threshold.
    Union-find still puts all three in one group — that is the documented
    connected-component semantics, and this test pins it down so nobody
    later assumes 'group means every pair within threshold'."""
    db = tmp_path / "photo.db"
    files = tmp_path / "files"
    conn = connect(db)
    try:
        # dHash identical for all three; the whole behavior is driven by pHash.
        A = "0000000000000000"           # all zeros
        B = "0000000000000001"           # 1 bit differs from A
        C = "00000000000001ff"           # 8 bits from B, 9 from A (> 8)
        assert hamming_distance(A, B) == 1
        assert hamming_distance(B, C) == 8
        assert hamming_distance(A, C) == 9

        _insert_hashed(conn, files, "a.jpg", "a" * 64, A, A)
        _insert_hashed(conn, files, "b.jpg", "b" * 64, B, A)
        _insert_hashed(conn, files, "c.jpg", "c" * 64, C, A)

        result = build_near_groups(conn)
        # Only A-B and B-C are candidate pairs.
        assert result["near_pairs"] == 2
        # But the cluster still contains all three members.
        assert result["near_groups"] == 1
        group = conn.execute(
            "SELECT * FROM groups WHERE kind='near'"
        ).fetchone()
        members = {
            row["rel_path"]
            for row in conn.execute(
                """
                SELECT p.rel_path FROM group_members gm
                JOIN photos p ON p.photo_id = gm.photo_id
                WHERE gm.group_id = ?
                """,
                (group["group_id"],),
            )
        }
        assert members == {"a.jpg", "b.jpg", "c.jpg"}
    finally:
        conn.close()


def test_exact_duplicates_are_excluded_from_near(tmp_path):
    """Byte-identical files belong to the exact stage only: they must not
    produce near pairs, so reports stop double-listing the same set."""
    db = tmp_path / "photo.db"
    files = tmp_path / "files"
    conn = connect(db)
    try:
        H = "1234567890abcdef"
        _insert_hashed(conn, files, "orig.jpg", "a" * 64, H, H)
        _insert_hashed(conn, files, "backup/copy.jpg", "a" * 64, H, H)  # same sha

        result = build_near_groups(conn)
        # Byte-identical files are exact-stage only: no near pairs at all.
        assert result["near_groups"] == 0
        assert result["near_pairs"] == 0
        assert result["near_members"] == 0
        assert conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0] == 0
    finally:
        conn.close()


def test_only_current_algorithm_version_is_grouped(tmp_path):
    """Hash rows written by an older algorithm version must not be grouped
    (they are stale). Only rows matching the current HASH_VERSION count."""
    db = tmp_path / "photo.db"
    files = tmp_path / "files"
    conn = connect(db)
    try:
        H = "1234567890abcdef"
        _insert_hashed(conn, files, "old.jpg", "a" * 64, H, H)
        # Simulate a pre-bump row: rewrite it under a stale version.
        old_id = conn.execute(
            "SELECT photo_id FROM photos WHERE rel_path='old.jpg'"
        ).fetchone()[0]
        conn.execute(
            "UPDATE photo_hashes SET algorithm_version='m1-v0' WHERE photo_id=?",
            (old_id,),
        )
        _insert_hashed(conn, files, "new.jpg", "b" * 64, H, H)

        result = build_near_groups(conn)
        # old.jpg is excluded, so no pair is formed.
        assert result["near_groups"] == 0
        assert result["near_pairs"] == 0
        assert result["near_members"] == 0
    finally:
        conn.close()


def test_photos_without_hashes_are_ignored(tmp_path):
    photos = tmp_path / "photos"
    photos.mkdir()
    # Valid image for hashing
    _make_noisy(photos / "ok.jpg", seed=3, quality=90)
    # Unreadable file: scan must not crash and the file gets no hashes row
    (photos / "broken.jpg").write_bytes(b"corrupt bytes")

    db = tmp_path / "photo.db"
    thumbs = tmp_path / "work" / "thumbs"
    conn = connect(db)
    try:
        stats = scan(photos, CFG, conn, thumbs)
        # The good file hashes cleanly; the broken file must not crash the
        # scan and must produce no hash row. It is now counted as
        # undecodable (no pixels -> row skipped) rather than hash_fail.
        assert stats["hash_ok"] == 1
        assert stats["hash_fail"] == 0
        assert stats["undecodable"] == 1
        # The broken file must not have a hash row (it was skipped entirely).
        assert conn.execute("SELECT COUNT(*) FROM photo_hashes").fetchone()[0] == 1

        result = build_near_groups(conn)
        assert result["near_groups"] == 0
    finally:
        conn.close()


def test_deleted_files_are_excluded_from_near(tmp_path):
    """Rows whose file no longer exists on disk must not participate in
    near grouping (stale DB history would otherwise produce ghost groups)."""
    db = tmp_path / "photo.db"
    files = tmp_path / "files"
    conn = connect(db)
    H = "1234567890abcdef"
    A = "0000000000000000"
    B = "0000000000000001"
    try:
        # a.jpg is near b.jpg on pHash (1 bit), but a.jpg is deleted after
        # registration: the near run must produce nothing.
        _insert_hashed(conn, files, "a.jpg", "a" * 64, A, A)
        _insert_hashed(conn, files, "b.jpg", "b" * 64, B, A)
        (files / "a.jpg").unlink()

        result = build_near_groups(conn)
        assert result["near_groups"] == 0
        assert result["near_pairs"] == 0
    finally:
        conn.close()


def test_stale_same_path_history_is_excluded_from_near(tmp_path):
    """When the same abs_path has an older DB record (old content at that
    path, kept by M0's append-only history) plus the current one, only the
    LATEST record may participate in near grouping."""
    db = tmp_path / "photo.db"
    files = tmp_path / "files"
    conn = connect(db)
    try:
        A = "0000000000000000"
        # Stale record: same abs_path, different sha (old content), hash
        # that is close to the current one's — must be ignored.
        stale_id, _ = upsert_photo(
            conn,
            rel_path="p.jpg",
            abs_path=str(files / "p.jpg"),
            sha256="deadbeef" * 8,
        )
        upsert_photo_hashes(conn, stale_id, A, A, HASH_VERSION)

        _insert_hashed(conn, files, "p.jpg", "b" * 64, "0000000000000001", A)
        _insert_hashed(conn, files, "q.jpg", "c" * 64, "0000000000000002", A)

        result = build_near_groups(conn)
        # p and q are 1 bit apart: exactly one near group, only live rows.
        assert result["near_groups"] == 1
        assert result["near_pairs"] == 1
    finally:
        conn.close()
