from __future__ import annotations

import sqlite3

import pytest

from database import SCHEMA_VERSION, connect


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
        ).fetchone()[0] == str(SCHEMA_VERSION)
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


def test_fresh_database_migrates_in_steps(tmp_path):
    # connect() on an empty path must end at the latest schema version
    # and photo_hashes must carry the M1 columns.
    db = tmp_path / "fresh.db"
    conn = connect(db)
    try:
        assert conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0] == str(SCHEMA_VERSION)
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(photo_hashes)")
        }
        assert {"photo_id", "phash", "dhash", "algorithm_version", "computed_at"} <= cols
    finally:
        conn.close()


def test_m0_database_is_upgraded_to_v2(tmp_path):
    """A database built by M0 (version 1) is upgraded in place to v2."""
    from pathlib import Path

    project = Path(__file__).resolve().parents[1]
    db = tmp_path / "m0.db"

    # Build a pure M0 database without going through database.connect()
    conn = sqlite3.connect(str(db))
    conn.executescript(
        (project / "migrations" / "001_initial.sql").read_text(encoding="utf-8")
    )
    conn.commit()
    conn.close()

    # Now open it through the project API: it must apply 002_m1.sql and
    # 003_m2.sql in sequence, ending at the latest schema version.
    conn = connect(db)
    try:
        assert conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0] == str(SCHEMA_VERSION)
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(photo_hashes)")
        }
        assert "algorithm_version" in cols
        assert "computed_at" in cols
    finally:
        conn.close()

