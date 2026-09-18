"""Stage 0: recursive scan -> SHA256 -> metadata -> thumbnail -> SQLite."""
from __future__ import annotations

import hashlib
import os
from collections import Counter
from pathlib import Path

from database import upsert_photo
from exif import read_photo_meta
from thumbnails import make_thumbnail

_CHUNK = 1 << 20


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def iter_image_files(root: Path, extensions: list, ignore_dirs: list):
    exts = {str(ext).lower().lstrip(".") for ext in extensions}
    ignored = {str(name) for name in ignore_dirs}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames if d not in ignored and not d.startswith(".")
        )
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            suffix = Path(name).suffix.lower().lstrip(".")
            if suffix and suffix in exts:
                yield Path(dirpath) / name


def scan(
    root: Path,
    cfg: dict,
    conn,
    thumbs_dir: Path,
    limit: int = 0,
) -> dict:
    inp = cfg.get("input") or {}
    thumb_cfg = cfg.get("thumbnail") or {}
    size = int(thumb_cfg.get("size", 256))
    stats: Counter = Counter({
        "inserted": 0,
        "updated": 0,
        "thumb_ok": 0,
        "thumb_fail": 0,
    })
    total = 0

    for path in iter_image_files(
        root,
        inp.get("extensions", []),
        inp.get("ignore_dirs", []),
    ):
        if limit and total >= limit:
            break

        rel = str(path.relative_to(root)).replace(os.sep, "/")
        meta = read_photo_meta(path)
        sha = sha256_file(path)

        photo_id, action = upsert_photo(
            conn,
            rel_path=rel,
            abs_path=str(path.resolve()),
            sha256=sha,
            size_bytes=meta.get("size_bytes"),
            width=meta.get("width"),
            height=meta.get("height"),
            fmt=meta.get("fmt"),
            taken_at=meta.get("taken_at"),
            gps_lat=meta.get("gps_lat"),
            gps_lng=meta.get("gps_lng"),
            camera_make=meta.get("camera_make"),
            camera_model=meta.get("camera_model"),
            file_mtime=meta.get("file_mtime"),
        )
        stats[action] += 1

        thumb_ok = make_thumbnail(
            path,
            thumbs_dir / f"{photo_id}.jpg",
            size,
        )
        stats["thumb_ok" if thumb_ok else "thumb_fail"] += 1

        total += 1
        if total % 200 == 0:
            conn.commit()
            print(f"  ... 已处理 {total} 张")

    conn.commit()
    stats["total"] = total
    return dict(stats)
