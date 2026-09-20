"""M1 perceptual image hashes (pHash / dHash).

Each image file is reduced to two 64-bit hashes, stored as hex strings:

- pHash: overall visual structure (DCT-based), robust to rescale/recompress
- dHash: adjacent pixel gradient structure

The hashes are bound to ``photo_id``. Since a photo's identity in M0 is
``(rel_path, sha256)``, changed content naturally yields a new record and a
fresh hash computation — no extra invalidation logic is needed.
"""
from __future__ import annotations

from pathlib import Path

import heif  # registers the HEIF opener (no-op if pillow-heif is missing)

try:
    from PIL import Image, ImageOps
    import imagehash
except ImportError:
    imagehash = None
    Image = None
    ImageOps = None

HASH_VERSION = "m1-v1"


def compute_hashes(path: str | Path) -> dict[str, str] | None:
    """Compute pHash/dHash for one image file.

    Returns ``{"phash": hex, "dhash": hex}`` or ``None`` when the file cannot
    be read/decoded (or the optional imagehash dependency is missing); the
    caller decides how to count the failure.
    """
    if imagehash is None or Image is None:
        return None
    try:
        with Image.open(path) as im:
            im.load()
            im = ImageOps.exif_transpose(im)
            return {
                "phash": str(imagehash.phash(im)),
                "dhash": str(imagehash.dhash(im)),
            }
    except Exception:
        return None
