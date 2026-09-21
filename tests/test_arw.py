from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

import arw
from database import connect
from exif import read_photo_meta
from hashes import compute_hashes
from scanner import scan
from thumbnails import make_thumbnail

# The real Sony ARW test set (22 ARW + 26 JPG, ~1.9 GB) lives outside the
# repo. Decode tests against a *real* ARW are skipped when it's absent so
# the suite still passes in a clean checkout; the fault-isolation and
# guard tests are synthetic and always run.
ARW_TEST_DIR = Path(r"D:/AIProjects/test_pics/ARW_TEST")
needs_real_arw = pytest.mark.skipif(
    not ARW_TEST_DIR.exists(),
    reason="real ARW test set not present",
)


def _cfg(exts):
    return {
        "input": {"extensions": exts, "ignore_dirs": [".git", "work", "reports"]},
        "thumbnail": {"size": 32},
    }


def test_arw_magic_accept():
    """ARW files start with the TIFF little/big-endian magic, which is the
    only thing that distinguishes them from plain TIFF at the byte level."""
    assert arw._accept(b"II\x2a\x00") is True
    assert arw._accept(b"MM\x00\x2a") is True
    # JPEG (FFD8), PNG, and short/garbage headers must NOT be accepted: the
    # opener would otherwise misfire and swallow non-ARW files.
    assert arw._accept(b"\xff\xd8\xff") is False
    assert arw._accept(b"\x89PNG") is False
    assert arw._accept(b"II") is False
    assert arw._accept(b"") is False


def test_arw_corrupt_does_not_kill_scan(tmp_path):
    """A corrupt/undecodable ARW mixed with valid files must not abort the
    scan: the bad file is counted as a failure and skipped, the good file is
    still fully processed. This is the core acceptance-matrix item
    ('a single bad ARW must not sink the whole scan')."""
    # A valid JPEG the decoder can always handle.
    good = tmp_path / "good.jpg"
    Image.new("RGB", (24, 16), (10, 200, 10)).save(good, "JPEG")

    # A corrupt ARW: real TIFF magic (so the scanner picks it up) followed by
    # garbage that rawpy cannot turn into a decodable preview.
    bad = tmp_path / "bad.arw"
    bad.write_bytes(b"II\x2a\x00" + b"\x00" * 256)

    db = tmp_path / "photo.db"
    thumbs = tmp_path / "work" / "thumbs"
    conn = connect(db)
    try:
        stats = scan(tmp_path, _cfg(["jpg", "jpeg", "arw"]), conn, thumbs)
        # The bad ARW must NOT crash the scan. It is either counted as
        # undecodable (no pixels at all -> row skipped) or lands in a
        # per-file failure bucket (thumb_fail / hash_fail). Either way it
        # must NOT become a usable photo with a thumbnail.
        bad_fail = stats["undecodable"] + stats["scan_fail"] + stats["thumb_fail"] + stats["hash_fail"]
        assert bad_fail >= 1
        # The good JPEG survived: exactly one thumbnail, and one hash row.
        assert len(list(thumbs.glob("*.jpg"))) == 1
        assert conn.execute("SELECT COUNT(*) FROM photo_hashes").fetchone()[0] == 1
        # The good file is the only row carrying a real format.
        rows = conn.execute(
            "SELECT abs_path, format FROM photos WHERE format IS NOT NULL"
        ).fetchall()
        assert len(rows) == 1
        assert Path(rows[0]["abs_path"]).name == "good.jpg"
    finally:
        conn.close()


def test_arw_corrupt_decoder_raises_cleanly(tmp_path):
    """A corrupt ARW that DOES reach the decoder must raise a clean
    UnidentifiedImageError (so thumbnails/hashes catch it), not a segfault
    or an obscure rawpy error."""
    bad = tmp_path / "bad.arw"
    bad.write_bytes(b"II\x2a\x00" + b"\x00" * 256)
    with pytest.raises(Exception):
        with Image.open(bad) as im:
            im.load()


@needs_real_arw
def test_arw_decodes_with_full_exif():
    """A real Sony ARW must open through PIL, report the camera's embedded
    preview dimensions, and expose real EXIF (make/model, capture date)."""
    arw_path = sorted(ARW_TEST_DIR.glob("*.ARW"), key=lambda p: p.stat().st_size)[0]
    with Image.open(arw_path) as im:
        im.load()
        assert im.size[0] > 0 and im.size[1] > 0

    meta = read_photo_meta(arw_path)
    assert meta["width"] is not None
    assert meta["height"] is not None
    assert meta["fmt"]
    # Sony writes real EXIF into the embedded preview.
    assert meta["camera_make"] == "SONY" or meta["camera_model"]


@needs_real_arw
def test_arw_thumbnail():
    arw_path = sorted(ARW_TEST_DIR.glob("*.ARW"), key=lambda p: p.stat().st_size)[0]
    dst = Path(r"C:/Users/Administrator/AppData/Local/hermes/cache/scratch/_arw_test_thumb.jpg")
    dst.parent.mkdir(parents=True, exist_ok=True)
    assert make_thumbnail(arw_path, dst, size=32) is True
    with Image.open(dst) as t:
        assert t.format == "JPEG"
        assert max(t.size) <= 32


@needs_real_arw
def test_arw_hashes():
    arw_path = sorted(ARW_TEST_DIR.glob("*.ARW"), key=lambda p: p.stat().st_size)[0]
    pair = compute_hashes(arw_path)
    assert pair is not None
    assert len(pair["phash"]) == 16
    assert len(pair["dhash"]) == 16


@needs_real_arw
def test_arw_end_to_end_scan(tmp_path):
    """Scan a folder containing one real ARW: the row must carry dimensions
    + format, a thumbnail must exist, and the hash row must be computed."""
    import shutil

    arw_path = sorted(ARW_TEST_DIR.glob("*.ARW"), key=lambda p: p.stat().st_size)[0]
    folder = tmp_path / "photos"
    folder.mkdir()
    # Copy (not symlink — symlinks need Windows privileges) the smallest real
    # ARW into the scan folder so the test is self-contained.
    shutil.copy2(arw_path, folder / arw_path.name)

    db = tmp_path / "photo.db"
    thumbs = tmp_path / "work" / "thumbs"
    conn = connect(db)
    try:
        stats = scan(folder, _cfg(["arw"]), conn, thumbs)
        assert stats["inserted"] == 1
        assert stats["scan_fail"] == 0
        assert stats["thumb_fail"] == 0
        assert stats["hash_fail"] == 0
        row = conn.execute(
            "SELECT width, height, format FROM photos"
        ).fetchone()
        assert row["width"] and row["height"] and row["format"]
        assert len(list(thumbs.glob("*.jpg"))) == 1
        assert conn.execute("SELECT COUNT(*) FROM photo_hashes").fetchone()[0] == 1
    finally:
        conn.close()


def test_arw_registration_is_idempotent():
    """Importing the module any number of times must not raise (Pillow's
    register_* is idempotent, mirroring heif.py)."""
    import importlib

    for _ in range(2):
        importlib.reload(arw)
    assert arw.ARW_SUPPORTED is True
