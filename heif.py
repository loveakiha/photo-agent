"""HEIC/HEIF support for Pillow.

``pillow-heif`` provides Pillow's HEIF decoder. Importing this module
registers the HEIF opener exactly once; every other module that decodes
images (exif, thumbnails, hashes) imports it so decoding works no matter
which module gets imported first. When the optional package is missing,
HEIC files simply fail to decode and are reported as failures — no hard
dependency.
"""
from __future__ import annotations

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF_SUPPORTED = True
except ImportError:
    pillow_heif = None
    HEIF_SUPPORTED = False
