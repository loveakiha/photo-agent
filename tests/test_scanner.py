from __future__ import annotations

from pathlib import Path

from PIL import Image

from database import connect
from hashes import HASH_VERSION
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


def test_first_scan_and_idempotent_rescan(tmp_path):
    photos = tmp_path / "photos"
    photos.mkdir()
    make_jpeg(photos / "a.jpg")
    make_jpeg(photos / "b.jpg", (0, 255, 0))

    db = tmp_path / "photo.db"
    thumbs = tmp_path / "work" / "thumbs"
    conn = connect(db)
    try:
        first = scan(photos, cfg(), conn, thumbs)
        second = scan(photos, cfg(), conn, thumbs)

        assert first["inserted"] == 2
        assert second["inserted"] == 0
        assert second["updated"] == 2
        assert conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 2
        assert len(list(thumbs.glob("*.jpg"))) == 2
    finally:
        conn.close()


def test_identical_files_at_different_paths_create_two_rows(tmp_path):
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
        result = scan(photos, cfg(), conn, thumbs)
        assert result["inserted"] == 2
        rows = conn.execute(
            "SELECT photo_id, rel_path, sha256 FROM photos ORDER BY rel_path"
        ).fetchall()
        assert len(rows) == 2
        assert {row["rel_path"] for row in rows} == {
            "backup/copy.jpg",
            "original.jpg",
        }
        assert rows[0]["sha256"] == rows[1]["sha256"]
        assert rows[0]["photo_id"] != rows[1]["photo_id"]
    finally:
        conn.close()


def test_same_path_changed_content_creates_new_row(tmp_path):
    photos = tmp_path / "photos"
    photos.mkdir()
    target = photos / "change.jpg"
    make_jpeg(target, (255, 0, 0))

    db = tmp_path / "photo.db"
    thumbs = tmp_path / "work" / "thumbs"
    conn = connect(db)
    try:
        first = scan(photos, cfg(), conn, thumbs)
        old_id = conn.execute(
            "SELECT photo_id FROM photos WHERE rel_path='change.jpg'"
        ).fetchone()[0]

        make_jpeg(target, (0, 0, 255))
        second = scan(photos, cfg(), conn, thumbs)

        assert first["inserted"] == 1
        assert second["inserted"] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM photos WHERE rel_path='change.jpg'"
        ).fetchone()[0] == 2
        ids = [
            row[0]
            for row in conn.execute(
                "SELECT photo_id FROM photos WHERE rel_path='change.jpg' ORDER BY photo_id"
            )
        ]
        assert old_id in ids
    finally:
        conn.close()


def test_hash_cache_respects_algorithm_version(tmp_path):
    """The P0 regression: a hash row from an OLDER algorithm version (e.g.
    NULL after an M0 -> M1 upgrade) must not count as cached — the scanner
    must recompute it. A row under the CURRENT version is skipped."""
    photos = tmp_path / "photos"
    photos.mkdir()
    make_jpeg(photos / "a.jpg")

    db = tmp_path / "photo.db"
    thumbs = tmp_path / "work" / "thumbs"
    conn = connect(db)
    try:
        first = scan(photos, cfg(), conn, thumbs)
        assert first["hash_ok"] == 1

        photo_id = conn.execute(
            "SELECT photo_id FROM photos WHERE rel_path='a.jpg'"
        ).fetchone()[0]

        # 1) Current-version row -> cached, not recomputed.
        second = scan(photos, cfg(), conn, thumbs)
        assert second["hash_ok"] == 0
        assert second["hash_skipped"] == 1

        # 2) Simulate a pre-bump row (NULL version, as M0 databases have
        #    after migration 002) -> must be recomputed, not skipped.
        conn.execute(
            "UPDATE photo_hashes SET algorithm_version=NULL WHERE photo_id=?",
            (photo_id,),
        )
        conn.commit()
        third = scan(photos, cfg(), conn, thumbs)
        assert third["hash_ok"] == 1
        assert third["hash_skipped"] == 0

        # 3) Row is now under the current version again.
        version = conn.execute(
            "SELECT algorithm_version FROM photo_hashes WHERE photo_id=?",
            (photo_id,),
        ).fetchone()[0]
        assert version == HASH_VERSION
    finally:
        conn.close()
