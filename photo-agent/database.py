"""SQLite connection, schema migration, and basic writes."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 1
_MIGRATIONS = Path(__file__).resolve().parent / "migrations"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def connect(db_path: str | Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    migrate(conn)
    return conn


def _current_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        return int(row["value"]) if row else 0
    except (sqlite3.OperationalError, TypeError, ValueError):
        return 0


def migrate(conn: sqlite3.Connection) -> int:
    """Apply M0 migrations idempotently."""
    current = _current_version(conn)
    if current >= SCHEMA_VERSION:
        return current
    sql = (_MIGRATIONS / "001_initial.sql").read_text(encoding="utf-8")
    conn.executescript(sql)
    conn.commit()
    return _current_version(conn)


def upsert_photo(
    conn: sqlite3.Connection,
    *,
    rel_path: str,
    abs_path: str,
    sha256: str,
    size_bytes=None,
    width=None,
    height=None,
    fmt=None,
    taken_at=None,
    gps_lat=None,
    gps_lng=None,
    camera_make=None,
    camera_model=None,
    file_mtime=None,
):
    """Insert or update a scanned file instance.

    The identity used for M0 idempotency is (rel_path, sha256), not sha256 alone.
    This allows two different paths containing byte-identical files to coexist.
    """
    row = conn.execute(
        "SELECT photo_id FROM photos WHERE rel_path = ? AND sha256 = ?",
        (rel_path, sha256),
    ).fetchone()

    if row is not None:
        conn.execute(
            """
            UPDATE photos
            SET abs_path = ?, size_bytes = ?, width = ?, height = ?, format = ?,
                taken_at = ?, gps_lat = ?, gps_lng = ?, camera_make = ?,
                camera_model = ?, file_mtime = ?, last_scanned = ?
            WHERE photo_id = ?
            """,
            (
                abs_path,
                size_bytes,
                width,
                height,
                fmt,
                taken_at,
                gps_lat,
                gps_lng,
                camera_make,
                camera_model,
                file_mtime,
                _now(),
                row["photo_id"],
            ),
        )
        return row["photo_id"], "updated"

    cur = conn.execute(
        """
        INSERT INTO photos
          (rel_path, abs_path, sha256, size_bytes, width, height, format,
           taken_at, gps_lat, gps_lng, camera_make, camera_model,
           file_mtime, first_seen, last_scanned)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            rel_path,
            abs_path,
            sha256,
            size_bytes,
            width,
            height,
            fmt,
            taken_at,
            gps_lat,
            gps_lng,
            camera_make,
            camera_model,
            file_mtime,
            _now(),
            _now(),
        ),
    )
    return cur.lastrowid, "inserted"


def add_group(conn: sqlite3.Connection, kind: str, rep_photo_id: int, size: int) -> int:
    cur = conn.execute(
        """
        INSERT INTO groups (kind, rep_photo_id, size, created_at)
        VALUES (?,?,?,?)
        """,
        (kind, rep_photo_id, size, _now()),
    )
    return cur.lastrowid


def add_group_member(
    conn: sqlite3.Connection,
    group_id: int,
    photo_id: int,
    recall_source: str,
    sim_to_rep: float | None = None,
):
    conn.execute(
        """
        INSERT OR REPLACE INTO group_members
          (group_id, photo_id, recall_source, sim_to_rep)
        VALUES (?,?,?,?)
        """,
        (group_id, photo_id, recall_source, sim_to_rep),
    )


def set_decision(
    conn: sqlite3.Connection,
    photo_id: int,
    status: str,
    final_score=None,
    reason: str | None = None,
    source: str = "algo",
    rule_version: str = "v1",
):
    conn.execute(
        """
        INSERT INTO decisions
          (photo_id, status, final_score, reason, source, rule_version, updated_at)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(photo_id) DO UPDATE SET
          status       = excluded.status,
          final_score  = excluded.final_score,
          reason       = excluded.reason,
          source       = excluded.source,
          rule_version = excluded.rule_version,
          updated_at   = excluded.updated_at
        """,
        (photo_id, status, final_score, reason, source, rule_version, _now()),
    )


def clear_exact_results(conn: sqlite3.Connection):
    """Clear previous algorithm-generated exact groups/decisions."""
    conn.execute(
        """
        DELETE FROM group_members
        WHERE group_id IN (
          SELECT group_id FROM groups WHERE kind='exact'
        )
        """
    )
    conn.execute("DELETE FROM groups WHERE kind='exact'")
    conn.execute(
        "DELETE FROM decisions WHERE status='DUPLICATE' AND source='algo'"
    )
