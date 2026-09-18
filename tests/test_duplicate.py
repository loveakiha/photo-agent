from __future__ import annotations

from pathlib import Path

from PIL import Image

from database import connect
from duplicate import build_exact_groups
from scanner import scan


def test_exact_duplicate_group(tmp_path):
    photos = tmp_path / "photos"
    backup = photos / "backup"
    backup.mkdir(parents=True)

    src = photos / "a.jpg"
    Image.new("RGB", (20, 20), (123, 45, 67)).save(src, "JPEG")
    (backup / "a-copy.jpg").write_bytes(src.read_bytes())

    db = tmp_path / "photo.db"
    thumbs = tmp_path / "work" / "thumbs"
    conn = connect(db)
    try:
        scan(
            photos,
            {
                "input": {"extensions": ["jpg"], "ignore_dirs": []},
                "thumbnail": {"size": 32},
            },
            conn,
            thumbs,
        )
        result = build_exact_groups(conn)
        assert result == {"exact_groups": 1, "duplicates": 1}

        group = conn.execute(
            "SELECT * FROM groups WHERE kind='exact'"
        ).fetchone()
        assert group["size"] == 2

        decisions = conn.execute(
            "SELECT photo_id, status FROM decisions"
        ).fetchall()
        assert len(decisions) == 1
        assert decisions[0]["status"] == "DUPLICATE"

        assert src.exists()
        assert (backup / "a-copy.jpg").exists()
    finally:
        conn.close()
