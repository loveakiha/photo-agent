"""M0 thumbnail generation. Originals are never modified."""
from __future__ import annotations

from pathlib import Path

import heif  # registers the HEIF opener (no-op if pillow-heif is missing)


def make_thumbnail(src: str | Path, dst: str | Path, size: int = 256) -> bool:
    dst = Path(dst)
    if dst.exists():
        return True
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image, ImageOps

        with Image.open(src) as im:
            im.load()
            im = ImageOps.exif_transpose(im)
            im.thumbnail((size, size))
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            im.save(dst, "JPEG", quality=85)
        return True
    except Exception:
        return False
