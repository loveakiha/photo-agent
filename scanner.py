"""Stage 0: recursive scan -> SHA256 -> metadata -> thumbnail -> SQLite."""
from __future__ import annotations

import hashlib
import os
from collections import Counter
from pathlib import Path

from database import (
    prune_missing_files,
    has_photo_hashes,
    upsert_photo,
    upsert_photo_hashes,
)
from exif import read_photo_meta
from hashes import HASH_VERSION, compute_hashes
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
        "moved": 0,
        "thumb_ok": 0,
        "thumb_fail": 0,
        "hash_ok": 0,
        "hash_fail": 0,
        "hash_skipped": 0,
        "scan_fail": 0,
        "undecodable": 0,
    })
    total = 0

    for path in iter_image_files(
        root,
        inp.get("extensions", []),
        inp.get("ignore_dirs", []),
    ):
        if limit and total >= limit:
            break

        # Per-file fault isolation: a single corrupt/undecodable file (a
        # truncated ARW, a renamed JPEG, a file that vanishes mid-scan, a DB
        # hiccup) must not abort the whole scan. Its expected failures are
        # already counted by the decoders (thumb_fail / hash_fail); this
        # guard additionally catches anything unexpected in the per-file
        # path, counts it as scan_fail, and continues with the next file.
        try:
            rel = str(path.relative_to(root)).replace(os.sep, "/")
            meta = read_photo_meta(path)

            # A file the decoder could not turn into pixels (a corrupt ARW,
            # a truncated RAW, an undecodable container) yields no dimensions
            # AND no format. read_photo_meta swallows the decode error and
            # returns such an all-None meta; inserting it would leave a
            # "zombie" row (width/format NULL) that later surfaces as a
            # broken photo. Skip the row and count it as undecodable instead.
            if meta.get("width") is None and meta.get("fmt") is None:
                stats["undecodable"] += 1
                continue

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

            # M1: perceptual hashes, cached per photo_id. A cache row is only
            # valid for the current algorithm version (HASH_VERSION); rows
            # left over from an older version (NULL after M0 -> M1 upgrade, or
            # a previous m1-vX) are recomputed and overwritten by the upsert.
            if has_photo_hashes(conn, photo_id, HASH_VERSION):
                stats["hash_skipped"] += 1
            else:
                pair = compute_hashes(path)
                if pair is None:
                    stats["hash_fail"] += 1
                else:
                    upsert_photo_hashes(
                        conn,
                        photo_id,
                        pair["phash"],
                        pair["dhash"],
                        HASH_VERSION,
                    )
                    stats["hash_ok"] += 1

            # Thumbnails are keyed by SHA-256, NOT by photo_id: photo_id is an
            # autoincrement integer that resets whenever the database is
            # rebuilt (and differs across libraries), so an id-keyed thumbnail
            # would collide with a stale file left over from a previous scan
            # and make_thumbnail would wrongly keep it. A content hash is a
            # stable identity: the same bytes always map to the same thumbnail,
            # and a changed file gets a fresh one.
            thumb_ok = make_thumbnail(
                path,
                thumbs_dir / f"{sha}.jpg",
                size,
            )
            stats["thumb_ok" if thumb_ok else "thumb_fail"] += 1

            total += 1
            if total % 200 == 0:
                conn.commit()
                print(f"  ... 已处理 {total} 张")
        except Exception:
            stats["scan_fail"] += 1
            continue

    # Files no longer present on disk (deleted, or a moved file whose old
    # row was never reconciled) are pruned so they can no longer appear as
    # exact-duplicate members in later runs.
    pruned = prune_missing_files(conn)
    conn.commit()
    stats["total"] = total
    stats["pruned"] = pruned
    return dict(stats)
