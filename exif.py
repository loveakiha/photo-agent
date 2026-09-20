"""Photo metadata reader using Pillow, with graceful fallback to mtime."""
from __future__ import annotations

import datetime
import os
from pathlib import Path

import heif  # registers the HEIF opener (no-op if pillow-heif is missing)


def _dms_to_float(dms, ref):
    try:
        d, m, s = (float(x) for x in dms)
        value = d + m / 60.0 + s / 3600.0
        return -value if ref in ("S", "W") else value
    except Exception:
        return None


def _norm_datetime(value):
    if not value:
        return None
    text = str(value).strip()
    date_part, _, time_part = text.partition(" ")
    text = date_part.replace(":", "-")
    if time_part:
        text += "T" + time_part.strip()
    try:
        return datetime.datetime.fromisoformat(text).isoformat(timespec="seconds")
    except ValueError:
        return str(value).strip()


def read_photo_meta(path: str | Path) -> dict:
    meta = dict(
        width=None,
        height=None,
        fmt=None,
        taken_at=None,
        gps_lat=None,
        gps_lng=None,
        camera_make=None,
        camera_model=None,
        size_bytes=None,
        file_mtime=None,
    )

    try:
        meta["size_bytes"] = os.path.getsize(path)
        meta["file_mtime"] = (
            datetime.datetime.fromtimestamp(os.path.getmtime(path))
            .astimezone()
            .isoformat(timespec="seconds")
        )
    except OSError:
        pass

    try:
        from PIL import Image

        with Image.open(path) as im:
            meta["width"], meta["height"] = im.size
            meta["fmt"] = (im.format or "").upper() or None
            exif = im.getexif()
            exif_ifd = exif.get_ifd(0x8769)
            taken = exif_ifd.get(36867) or exif.get(306)
            meta["taken_at"] = _norm_datetime(taken)
            meta["camera_make"] = exif.get(271)
            meta["camera_model"] = exif.get(272)
            gps = exif.get_ifd(0x8825)
            if gps:
                meta["gps_lat"] = _dms_to_float(gps.get(2), gps.get(1))
                meta["gps_lng"] = _dms_to_float(gps.get(4), gps.get(3))
    except Exception:
        pass

    if meta["taken_at"] is None and meta["file_mtime"]:
        meta["taken_at"] = meta["file_mtime"]
    return meta
