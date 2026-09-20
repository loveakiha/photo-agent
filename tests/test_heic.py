from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

import heif
from database import connect
from exif import read_photo_meta
from hashes import compute_hashes
from scanner import scan
from thumbnails import make_thumbnail


def make_heic(path: Path, color=(255, 0, 0)):
    Image.new("RGB", (64, 48), color).save(path, "HEIF")


def cfg():
    return {
        "input": {
            "extensions": ["jpg", "jpeg", "png", "heic"],
            "ignore_dirs": [".git", "work", "reports"],
        },
        "thumbnail": {"size": 32},
    }


def test_heic_thumbnail(tmp_path):
    """An HEIC file must produce a JPEG thumbnail (the reported bug was
    that all HEIC files came back without thumbnails)."""
    src = tmp_path / "shot.heic"
    make_heic(src)
    dst = tmp_path / "thumbs" / "shot.jpg"

    assert make_thumbnail(src, dst, size=32) is True
    assert dst.exists()
    with Image.open(dst) as thumb:
        assert thumb.format == "JPEG"
        assert max(thumb.size) <= 32


def test_heic_metadata(tmp_path):
    src = tmp_path / "shot.heic"
    make_heic(src)

    meta = read_photo_meta(src)
    assert meta["width"] == 64
    assert meta["height"] == 48
    # The DB used to show format=None for HEIC rows.
    assert meta["fmt"]


def test_heic_hashes(tmp_path):
    src = tmp_path / "shot.heic"
    make_heic(src)

    pair = compute_hashes(src)
    assert pair is not None
    assert len(pair["phash"]) == 16
    assert len(pair["dhash"]) == 16


def test_heic_end_to_end_scan(tmp_path):
    """Scan a folder containing only an HEIC file: the row must carry
    dimensions + format, a thumbnail file must exist, and the hash row
    must be computed — no silent failures."""
    photos = tmp_path / "photos"
    photos.mkdir()
    make_heic(photos / "shot.heic")

    db = tmp_path / "photo.db"
    thumbs = tmp_path / "work" / "thumbs"
    conn = connect(db)
    try:
        stats = scan(photos, cfg(), conn, thumbs)
        assert stats["thumb_fail"] == 0
        assert stats["hash_fail"] == 0

        row = conn.execute(
            "SELECT width, height, format, abs_path FROM photos"
        ).fetchone()
        assert row["width"] == 64
        assert row["height"] == 48
        assert row["format"]

        assert len(list(thumbs.glob("*.jpg"))) == 1
        assert (
            conn.execute("SELECT COUNT(*) FROM photo_hashes").fetchone()[0]
            == 1
        )
    finally:
        conn.close()


def test_heif_registration_is_idempotent():
    """Importing the shared module any number of times must not raise."""
    import importlib

    for _ in range(2):
        importlib.reload(heif)
    assert heif.HEIF_SUPPORTED is True
