from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageOps

from thumbnails import make_thumbnail


def test_thumbnail_size_and_orientation(tmp_path):
    src = tmp_path / "src.jpg"
    dst = tmp_path / "thumb.jpg"

    # Portrait source. EXIF orientation=6 means rotate 90° clockwise.
    image = Image.new("RGB", (40, 20), "white")
    exif = image.getexif()
    exif[274] = 6
    image.save(src, "JPEG", exif=exif)

    assert make_thumbnail(src, dst, size=32) is True
    assert dst.exists()

    with Image.open(dst) as thumb:
        assert max(thumb.size) <= 32
        # Orientation correction turns the 40x20 source into 20x40.
        assert thumb.height > thumb.width
        assert thumb.mode == "RGB"


def test_existing_thumbnail_is_reused(tmp_path):
    src = tmp_path / "src.jpg"
    dst = tmp_path / "thumb.jpg"
    Image.new("RGB", (10, 10), "red").save(src, "JPEG")
    Image.new("RGB", (2, 2), "blue").save(dst, "JPEG")

    assert make_thumbnail(src, dst, size=32) is True
    with Image.open(dst) as thumb:
        assert thumb.size == (2, 2)
