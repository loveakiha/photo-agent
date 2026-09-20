"""SQLite connection, schema migration, and basic writes."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 2
_MIGRATIONS = Path(__file__).resolve().parent / "migrations"
# version -> migration script; applied once per version, in ascending order
_MIGRATION_SCRIPTS = {
    1: "001_initial.sql",
    2: "002_m1.sql",
}


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
    """Apply pending migrations in version order. Each script runs at most
    once, guarded by the schema_version meta row (the scripts themselves
    are not idempotent)."""
    current = _current_version(conn)
    if current >= SCHEMA_VERSION:
        return current
    for version in range(current + 1, SCHEMA_VERSION + 1):
        script = _MIGRATION_SCRIPTS[version]
        conn.executescript((_MIGRATIONS / script).read_text(encoding="utf-8"))
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


def has_photo_hashes(
    conn: sqlite3.Connection,
    photo_id: int,
    algorithm_version: str,
) -> bool:
    """True only if a complete hash row exists for the CURRENT algorithm
    version. Rows from an older version (e.g. NULL after an M0 -> M1
    upgrade, or 'm1-v1' after bumping to 'm1-v2') force recomputation."""
    row = conn.execute(
        """
        SELECT 1
        FROM photo_hashes
        WHERE photo_id = ?
          AND algorithm_version = ?
          AND phash IS NOT NULL
          AND dhash IS NOT NULL
        """,
        (photo_id, algorithm_version),
    ).fetchone()
    return row is not None


def upsert_photo_hashes(
    conn: sqlite3.Connection,
    photo_id: int,
    phash: str,
    dhash: str,
    algorithm_version: str,
):
    conn.execute(
        """
        INSERT INTO photo_hashes
          (photo_id, phash, dhash, algorithm_version, computed_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(photo_id) DO UPDATE SET
          phash = excluded.phash,
          dhash = excluded.dhash,
          algorithm_version = excluded.algorithm_version,
          computed_at = excluded.computed_at
        """,
        (photo_id, phash, dhash, algorithm_version, _now()),
    )


def get_photo_hashes(conn: sqlite3.Connection, photo_id: int) -> dict | None:
    row = conn.execute(
        "SELECT phash, dhash FROM photo_hashes WHERE photo_id = ?",
        (photo_id,),
    ).fetchone()
    if row is None:
        return None
    return {"phash": row["phash"], "dhash": row["dhash"]}


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


def clear_near_results(conn: sqlite3.Connection):
    """Clear previous algorithm-generated near groups (M1 candidates only,
    no decisions are written by the near stage)."""
    conn.execute(
        """
        DELETE FROM group_members
        WHERE group_id IN (
          SELECT group_id FROM groups WHERE kind='near'
        )
        """
    )
    conn.execute("DELETE FROM groups WHERE kind='near'")
