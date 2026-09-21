"""ARW (Sony Alpha RAW) support for Pillow.

An ARW file is a TIFF container holding a demosaiced full-frame RAW sensor
image plus, crucially, an **embedded JPEG preview** the camera itself writes
(a smaller, colour-managed render with the full EXIF block: date, make,
model, orientation). photo-agent does not need the sensor data to enter the
M0/M1/M2 pipeline — thumbnails, perceptual hashes, EXIF metadata and
technical-quality measurement all work from the embedded preview, exactly
like HEIC works from ``pillow-heif``.

So this module registers a Pillow opener that:
  - is only offered for files whose header is the TIFF magic ``II*/MM*``
    (the ``.arw`` extension is also registered, so PIL routes ``*.arw`` here
    first and plain JPEG/PNG/WebP are never touched),
  - opens the file with ``rawpy`` (the LibRaw Python binding) and returns the
    embedded JPEG preview as a loaded ``Image``, tagged ``format="ARW"``.

A file that is NOT a decodable RAW (a truncated ARW, a renamed JPEG, etc.)
raises ``UnidentifiedImageError`` — the same failure Pillow gives for an
undecodable image — so the scanner's per-file guard counts it as a failure
and continues rather than crashing the whole scan.

The optional ``rawpy`` dependency mirrors the HEIC story: when it is missing
ARW files simply fail to decode and are reported as failures, and importing
this module is always a no-op that does not raise.
"""
from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

try:
    import rawpy

    ARW_SUPPORTED = True
except ImportError:
    rawpy = None
    ARW_SUPPORTED = False


def _accept(data: bytes) -> bool:
    """Only offer the opener for a TIFF-magic header (ARW is TIFF-based).

    Plain JPEG (``FF D8``), PNG, WebP, etc. all fail this check, so their
    built-in openers are unaffected. A genuine ARW passes and is then
    handed to rawpy, which raises if it is not a real RAW.
    """
    return len(data) >= 4 and data[:2] in (b"II", b"MM")


def _arw_open(fp, filename, *args, **kwargs) -> Image.Image:
    """Pillow opener factory for ARW. Returns the embedded JPEG preview."""
    raw = rawpy.RawPy()
    try:
        raw.open_file(str(filename))
        thumb = raw.extract_thumb()
        preview = Image.open(io.BytesIO(bytes(thumb.data)))
        preview.load()
        preview.format = "ARW"
        return preview
    except Exception:
        raise Image.UnidentifiedImageError(f"ARW not decodable: {filename}")
    finally:
        raw.close()


def sensor_size(path: str | Path) -> tuple[int, int] | None:
    """Full demosaiced sensor dimensions of an ARW (e.g. ``(5184, 3464)``).

    This is the *sensor* resolution, not the embedded preview's. It is
    provided for reporting/VLM use; the M0/M1/M2 pipeline works from the
    preview and does not require calling this. Returns ``None`` when
    ``rawpy`` is missing or the file is not a decodable RAW.
    """
    if rawpy is None:
        return None
    raw = rawpy.RawPy()
    try:
        raw.open_file(str(path))
        sizes = raw.sizes
        return (int(sizes.width), int(sizes.height))
    except Exception:
        return None
    finally:
        raw.close()


_registered = False


def _register():
    """Register the ARW opener exactly once (idempotent)."""
    global _registered
    if _registered or rawpy is None:
        return
    Image.register_open("ARW", _arw_open, _accept)
    Image.register_extensions("ARW", [".arw"])
    _registered = True


_register()
