"""Contact sheet tests: one grid image per group, labeled cells."""
from __future__ import annotations

import pytest

from contact_sheets import build_contact_sheets
from database import connect, upsert_photo, upsert_photo_hashes
from hashes import HASH_VERSION
from PIL import Image
from similarity import hamming_distance


def _hash_at(hex_a: str, dist: int) -> str:
    bits_a = int(hex_a, 16)
    bits_b = bits_a ^ 0xFFFFFFFFFFFFFFFF
    delta = bits_b ^ (1 << dist)
    return f"{delta & 0xFFFFFFFFFFFFFFFF:016x}"


def test_one_sheet_per_near_group(tmp_path):
    conn = connect(tmp_path / "test.db")
    photos = []
    for i in range(4):
        path = tmp_path / f"img{i}.jpg"
        Image.new("RGB", (300, 200), color=(200 - i * 20, 60, 60)).save(path, "JPEG")
        pid, _ = upsert_photo(conn, rel_path=f"img{i}.jpg", abs_path=str(path), sha256=f"sha{i}")
        photos.append(pid)

    # all near each other: small distances from a base hash
    base = "0" * 16
    conn.execute("PRAGMA foreign_keys=ON")
    for i, pid in enumerate(photos):
        conn.execute(
            "INSERT INTO photo_hashes (photo_id, phash, dhash, algorithm_version) VALUES (?,?,?,?)",
            (pid, _hash_at(base, i), _hash_at(base, i), HASH_VERSION),
        )
    conn.execute(
        "INSERT INTO groups (group_id, kind, rep_photo_id) VALUES (1, 'near', ?)",
        (photos[0],),
    )
    for pid in photos:
        conn.execute("INSERT INTO group_members (group_id, photo_id) VALUES (1, ?)", (pid,))
    conn.commit()

    sheets = build_contact_sheets(conn, tmp_path / "thumbs", tmp_path / "contact", kind="near")
    conn.close()
    assert len(sheets) == 1
    assert sheets[0].name == "near_G001.jpg"
    assert sheets[0].exists()
    with Image.open(sheets[0]) as im:
        assert im.size[0] > 0 and im.size[1] > 0


def test_missing_thumbs_become_placeholders(tmp_path):
    """No thumbnails on disk → sheet is still built with placeholder cells."""
    conn = connect(tmp_path / "test.db")
    photos = []
    for i in range(2):
        path = tmp_path / f"img{i}.jpg"
        path.touch()  # file exists (near requires this) but no real image
        pid, _ = upsert_photo(conn, rel_path=f"img{i}.jpg", abs_path=str(path), sha256=f"sha{i}")
        photos.append(pid)
    base = "0" * 16
    for i, pid in enumerate(photos):
        conn.execute(
            "INSERT INTO photo_hashes (photo_id, phash, dhash, algorithm_version) VALUES (?,?,?,?)",
            (pid, _hash_at(base, i), _hash_at(base, i), HASH_VERSION),
        )
    conn.execute("INSERT INTO groups (group_id, kind, rep_photo_id) VALUES (1, 'near', ?)", (photos[0],))
    for pid in photos:
        conn.execute("INSERT INTO group_members (group_id, photo_id) VALUES (1, ?)", (pid,))
    conn.commit()

    sheets = build_contact_sheets(conn, tmp_path / "thumbs", tmp_path / "contact", kind="all")
    conn.close()
    assert len(sheets) == 1  # kind='all' still finds the near group


def test_no_groups_no_sheets(tmp_path):
    conn = connect(tmp_path / "test.db")
    path = tmp_path / "img0.jpg"
    Image.new("RGB", (10, 10)).save(path, "JPEG")
    upsert_photo(conn, rel_path="img0.jpg", abs_path=str(path), sha256="sha0")
    conn.commit()
    sheets = build_contact_sheets(conn, tmp_path / "thumbs", tmp_path / "contact", kind="all")
    conn.close()
    assert sheets == []
