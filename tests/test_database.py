from __future__ import annotations

import sqlite3

import pytest

from database import connect


def test_schema_created(tmp_path):
    db = tmp_path / "test.db"
    conn = connect(db)
    try:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        expected = {
            "meta",
            "photos",
            "photo_hashes",
            "embeddings",
            "quality",
            "vlm_sheets",
            "vlm_analysis",
            "groups",
            "group_members",
            "decisions",
            "runs",
        }
        assert expected <= tables
        assert conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0] == "1"
    finally:
        conn.close()


def test_foreign_keys_enabled_and_enforced(tmp_path):
    db = tmp_path / "test.db"
    conn = connect(db)
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO quality(photo_id) VALUES (9999)")
    finally:
        conn.close()
