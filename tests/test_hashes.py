from __future__ import annotations

from pathlib import Path

from PIL import Image

from database import connect, get_photo_hashes, upsert_photo_hashes
from hashes import HASH_VERSION, compute_hashes
from similarity import hamming_distance


def _make(path: Path, color=(255, 0, 0)):
    Image.new("RGB", (64, 48), color).save(path, "JPEG")


def test_identical_files_get_identical_hashes(tmp_path):
    a, b = tmp_path / "a.jpg", tmp_path / "b.jpg"
    _make(a)
    b.write_bytes(a.read_bytes())

    ha, hb = compute_hashes(a), compute_hashes(b)
    assert ha is not None and hb is not None
    assert ha["phash"] == hb["phash"]
    assert ha["dhash"] == hb["dhash"]


def test_clearly_different_images_are_far_apart(tmp_path):
    # Uniform images (black vs white) both hash to all-zeros, so use
    # structurally different content instead: vertical vs horizontal
    # gradients have opposite dHash signatures.
    a, b = tmp_path / "vgrad.jpg", tmp_path / "hgrad.jpg"
    size = 96
    vgrad = Image.new("RGB", (size, size))
    vgrad.putdata(
        [((int(255 * y / size),) * 3) for y in range(size) for _ in range(size)]
    )
    vgrad.save(a, "JPEG", quality=95)
    hgrad = Image.new("RGB", (size, size))
    hgrad.putdata(
        [((int(255 * x / size),) * 3) for _ in range(size) for x in range(size)]
    )
    hgrad.save(b, "JPEG", quality=95)

    ha, hb = compute_hashes(a), compute_hashes(b)
    # dHash compares horizontally adjacent pixels: vertical gradient -> all
    # 0s, horizontal gradient -> all 1s.
    assert hamming_distance(ha["dhash"], hb["dhash"]) >= 50
    assert hamming_distance(ha["phash"], hb["phash"]) > 0


def test_hashes_are_hex_strings(tmp_path):
    a = tmp_path / "a.jpg"
    _make(a)
    result = compute_hashes(a)
    int(result["phash"], 16)
    int(result["dhash"], 16)


def test_compute_hashes_unreadable_file_returns_none(tmp_path):
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"not an image at all")
    assert compute_hashes(broken) is None


def test_upsert_and_get_roundtrip(tmp_path):
    db = tmp_path / "photo.db"
    conn = connect(db)
    try:
        photo_id, _ = _insert_photo(conn, "a.jpg")
        assert get_photo_hashes(conn, photo_id) is None

        upsert_photo_hashes(
            conn, photo_id, "aa00bb11cc22dd33", "ff00ee11dd22cc33",
            HASH_VERSION,
        )
        row = get_photo_hashes(conn, photo_id)
        assert row == {
            "phash": "aa00bb11cc22dd33",
            "dhash": "ff00ee11dd22cc33",
        }
        # upsert updates, does not duplicate
        upsert_photo_hashes(
            conn, photo_id, "bb11cc22dd33ee44", "cc22dd33ee44ff55",
            HASH_VERSION,
        )
        assert get_photo_hashes(conn, photo_id) == {
            "phash": "bb11cc22dd33ee44",
            "dhash": "cc22dd33ee44ff55",
        }
    finally:
        conn.close()


def _insert_photo(conn, rel_path: str) -> tuple[int, str]:
    from database import upsert_photo

    return upsert_photo(
        conn,
        rel_path=rel_path,
        abs_path=str(Path(rel_path).resolve()),
        sha256="0" * 64,
    )
