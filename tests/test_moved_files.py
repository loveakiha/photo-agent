from __future__ import annotations

from pathlib import Path

from PIL import Image

from database import connect, prune_missing_files
from scanner import scan


def make_jpeg(path: Path, color=(255, 0, 0)):
    Image.new("RGB", (64, 48), color).save(path, "JPEG")


def cfg():
    return {
        "input": {
            "extensions": ["jpg", "jpeg", "png"],
            "ignore_dirs": [".git", "work", "reports"],
        },
        "thumbnail": {"size": 32},
    }


def test_moved_file_updates_existing_row(tmp_path):
    """A file moved inside the scanned root must update its existing row
    in place (keeping the photo_id and first_seen), not insert a new row
    that would later be flagged as an exact duplicate."""
    photos = tmp_path / "photos"
    sub = photos / "tested"
    sub.mkdir(parents=True)
    make_jpeg(photos / "a.jpg")

    db = tmp_path / "photo.db"
    thumbs = tmp_path / "work" / "thumbs"
    conn = connect(db)
    try:
        first = scan(photos, cfg(), conn, thumbs)
        assert first["inserted"] == 1
        old_id, first_seen = conn.execute(
            "SELECT photo_id, first_seen FROM photos WHERE rel_path='a.jpg'"
        ).fetchone()

        # Move the file inside the root and scan again.
        (photos / "a.jpg").rename(sub / "a.jpg")
        second = scan(photos, cfg(), conn, thumbs)

        assert second["moved"] == 1
        assert second["inserted"] == 0
        assert conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 1
        row = conn.execute(
            "SELECT photo_id, rel_path, first_seen FROM photos"
        ).fetchone()
        assert row["photo_id"] == old_id  # same record, not a new one
        assert row["rel_path"] == "tested/a.jpg"
        assert row["first_seen"] == first_seen  # history survives the move
    finally:
        conn.close()


def test_moved_file_keeps_its_hashes(tmp_path):
    """Move reconciliation must not lose the perceptual hashes of the
    moved file: upsert keeps the photo_id, so cached hashes stay valid."""
    photos = tmp_path / "photos"
    sub = photos / "tested"
    sub.mkdir(parents=True)
    make_jpeg(photos / "a.jpg")

    db = tmp_path / "photo.db"
    thumbs = tmp_path / "work" / "thumbs"
    conn = connect(db)
    try:
        scan(photos, cfg(), conn, thumbs)
        (photos / "a.jpg").rename(sub / "a.jpg")
        second = scan(photos, cfg(), conn, thumbs)
        # The hash row is still bound to the same photo_id -> cached.
        assert second["hash_skipped"] == 1
        assert second["hash_ok"] == 0
    finally:
        conn.close()


def test_deleted_file_is_pruned(tmp_path):
    """A file that disappears between scans leaves no stale row behind,
    so it can no longer surface as a duplicate member."""
    photos = tmp_path / "photos"
    photos.mkdir()
    make_jpeg(photos / "a.jpg")
    make_jpeg(photos / "b.jpg", (0, 255, 0))

    db = tmp_path / "photo.db"
    thumbs = tmp_path / "work" / "thumbs"
    conn = connect(db)
    try:
        scan(photos, cfg(), conn, thumbs)
        (photos / "b.jpg").unlink()

        pruned = prune_missing_files(conn)
        assert pruned == 1
        remaining = conn.execute("SELECT rel_path FROM photos").fetchall()
        assert [row[0] for row in remaining] == ["a.jpg"]
        # Child rows of the pruned photo are gone too.
        assert (
            conn.execute("SELECT COUNT(*) FROM photo_hashes").fetchone()[0] == 1
        )
    finally:
        conn.close()


def test_deleted_rep_with_members_purges_cleanly(tmp_path):
    """Regression: a stale photo that is a member/rep of an existing
    group (e.g. from a previous dedup run) must be purged without an
    FK violation — children and groups go before the photos row."""
    photos = tmp_path / "photos"
    photos.mkdir()
    make_jpeg(photos / "a.jpg")

    db = tmp_path / "photo.db"
    thumbs = tmp_path / "work" / "thumbs"
    conn = connect(db)
    try:
        scan(photos, cfg(), conn, thumbs)
        photo_id = conn.execute(
            "SELECT photo_id FROM photos WHERE rel_path='a.jpg'"
        ).fetchone()[0]
        # Simulate a previous dedup run: a group with this photo as rep +
        # member, and a DUPLICATE decision.
        from database import add_group, add_group_member, set_decision

        group_id = add_group(conn, "exact", photo_id, 1)
        add_group_member(conn, group_id, photo_id, "exact")
        set_decision(conn, photo_id, "DUPLICATE", source="algo")
        conn.commit()

        # The file vanishes; prune must drop the whole chain without an FK.
        (photos / "a.jpg").unlink()
        pruned = prune_missing_files(conn)
        assert pruned == 1
        assert conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM group_members").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0
    finally:
        conn.close()


def test_copy_and_original_coexist_then_copy_removed(tmp_path):
    """Byte-identical copies at two live paths stay two rows. When one
    copy is deleted, only the surviving row remains."""
    photos = tmp_path / "photos"
    backup = photos / "backup"
    backup.mkdir(parents=True)
    src = photos / "original.jpg"
    make_jpeg(src, (1, 2, 3))
    (backup / "copy.jpg").write_bytes(src.read_bytes())

    db = tmp_path / "photo.db"
    thumbs = tmp_path / "work" / "thumbs"
    conn = connect(db)
    try:
        first = scan(photos, cfg(), conn, thumbs)
        assert first["inserted"] == 2

        (backup / "copy.jpg").unlink()
        second = scan(photos, cfg(), conn, thumbs)
        # The surviving row is updated in place; the vanished copy's row is
        # purged during the upsert (the end-of-scan prune pass then finds
        # nothing left to do).
        assert second["moved"] == 0
        assert second["updated"] == 1
        assert second["pruned"] == 0
        rows = conn.execute(
            "SELECT rel_path FROM photos ORDER BY rel_path"
        ).fetchall()
        assert [row[0] for row in rows] == ["original.jpg"]
    finally:
        conn.close()
