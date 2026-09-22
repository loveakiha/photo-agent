"""SQLite connection, schema migration, and basic writes."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 6
_MIGRATIONS = Path(__file__).resolve().parent / "migrations"
# version -> migration script; applied once per version, in ascending order
_MIGRATION_SCRIPTS = {
    1: "001_initial.sql",
    2: "002_m1.sql",
    3: "003_m2.sql",
    4: "004_preference_tables.sql",
    5: "005_semantic.sql",
    6: "006_semantic_profile.sql",
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


def _vanished_rows(
    conn: sqlite3.Connection, rel_path: str, abs_path: str, sha256: str
) -> list:
    """Rows for the same sha256 at OTHER paths whose file no longer
    exists on disk — i.e. the file was moved (or a copied original was
    deleted). Ordered by photo_id (oldest first)."""
    rows = conn.execute(
        "SELECT photo_id, abs_path FROM photos WHERE sha256 = ? AND rel_path <> ? "
        "ORDER BY photo_id",
        (sha256, rel_path),
    ).fetchall()
    return [
        row
        for row in rows
        if row["abs_path"] != abs_path and not Path(row["abs_path"]).is_file()
    ]


def _purge_rows(conn: sqlite3.Connection, photo_ids: list):
    """Delete photos rows AND all dependent rows for the given ids.

    Children are removed before parents because ``PRAGMA foreign_keys`` is
    ON for these connections.
    """
    if not photo_ids:
        return
    marks = ",".join("?" for _ in photo_ids)
    for table in (
        "photo_hashes",
        "embeddings",
        "quality",
        "vlm_analysis",
        "semantic_analysis",
        "decisions",
    ):
        try:
            conn.execute(f"DELETE FROM {table} WHERE photo_id IN ({marks})", photo_ids)
        except sqlite3.OperationalError:
            continue  # table not present in this schema version
    # preference_samples references photos through THREE columns
    # (candidate_a/candidate_b/winner), not a single photo_id.
    try:
        conn.execute(
            f"DELETE FROM preference_samples WHERE "
            f"candidate_a IN ({marks}) OR candidate_b IN ({marks}) "
            f"OR winner IN ({marks})",
            photo_ids * 3,
        )
    except sqlite3.OperationalError:
        pass  # table not present in this schema version
    # group_members references BOTH groups and photos; groups references
    # photos. Order: members of the doomed groups -> the groups the purged
    # photos represent -> the photos rows. (groups/groups_members are
    # rebuilt by the dedup/near stages on every run, so dropping the whole
    # group is safe.)
    try:
        conn.execute(
            "DELETE FROM group_members WHERE group_id IN "
            f"(SELECT group_id FROM groups WHERE rep_photo_id IN ({marks}))",
            photo_ids,
        )
        conn.execute(f"DELETE FROM groups WHERE rep_photo_id IN ({marks})", photo_ids)
    except sqlite3.OperationalError:
        pass  # tables not present in this schema version
    conn.execute(f"DELETE FROM photos WHERE photo_id IN ({marks})", photo_ids)
    # Any remaining orphan members (members of groups whose OTHER members
    # survived) are rebuilt by the next dedup/near run — drop them too.
    try:
        conn.execute(
            "DELETE FROM group_members WHERE photo_id NOT IN (SELECT photo_id FROM photos)"
        )
        conn.execute(
            "DELETE FROM groups WHERE rep_photo_id NOT IN (SELECT photo_id FROM photos)"
        )
    except sqlite3.OperationalError:
        pass


def prune_missing_files(conn: sqlite3.Connection) -> int:
    """Delete photo rows whose file no longer exists on disk.

    A file instance that has been moved is reconciled by ``upsert_photo``
    (its row is updated in place when the new location is scanned). Rows
    that reach the end of a scan still pointing at a path that does not
    exist are stale (deleted files, or files removed since the previous
    scan) and are removed along with their dependent rows. Returns the
    number of photo rows deleted.
    """
    rows = conn.execute("SELECT photo_id, abs_path FROM photos").fetchall()
    stale_ids = [
        row["photo_id"] for row in rows if not Path(row["abs_path"]).is_file()
    ]
    if not stale_ids:
        return 0
    _purge_rows(conn, stale_ids)
    conn.commit()
    return len(stale_ids)


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

    Move reconciliation: a row at another path is treated as the SAME file
    having been moved only when that file has actually vanished from disk.
    - No row at (rel_path, sha256) + vanished row(s) elsewhere: the oldest
      vanished row is updated in place to the new path (first_seen kept, so
      the file's history survives a move) and the other vanished rows are
      purged. Action = "moved".
    - Row at (rel_path, sha256) + vanished row(s) elsewhere: the current row
      is updated and the vanished rows are purged (a copy was deleted).
    """
    meta = (
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
    )
    update_sql = """
        UPDATE photos
        SET abs_path = ?, size_bytes = ?, width = ?, height = ?, format = ?,
            taken_at = ?, gps_lat = ?, gps_lng = ?, camera_make = ?,
            camera_model = ?, file_mtime = ?, last_scanned = ?
        WHERE photo_id = ?
    """

    row = conn.execute(
        "SELECT photo_id FROM photos WHERE rel_path = ? AND sha256 = ?",
        (rel_path, sha256),
    ).fetchone()
    stale = _vanished_rows(conn, rel_path, abs_path, sha256)

    if row is not None:
        _purge_rows(conn, [r["photo_id"] for r in stale])
        conn.execute(update_sql, (*meta, row["photo_id"]))
        return row["photo_id"], "updated"

    if stale:
        kept = stale[0]["photo_id"]
        _purge_rows(conn, [r["photo_id"] for r in stale[1:]])
        conn.execute(
            f"""
            UPDATE photos
            SET rel_path = ?, abs_path = ?, size_bytes = ?, width = ?,
                height = ?, format = ?, taken_at = ?, gps_lat = ?, gps_lng = ?,
                camera_make = ?, camera_model = ?, file_mtime = ?,
                last_scanned = ?
            WHERE photo_id = ?
            """,
            (
                rel_path,
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
                kept,
            ),
        )
        return kept, "moved"

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


def has_quality(
    conn: sqlite3.Connection,
    photo_id: int,
    algorithm_version: str,
    sha256: str,
) -> bool:
    """True only if a complete quality row exists for the CURRENT algorithm
    version AND the same content hash. A row from an older version, or one
    bound to different bytes (a file that changed since), forces
    recomputation — same invalidation convention as M1 photo_hashes."""
    row = conn.execute(
        """
        SELECT 1
        FROM quality
        WHERE photo_id = ?
          AND algorithm_version = ?
          AND sha256 = ?
          AND sharpness_raw IS NOT NULL
          AND exposure_raw IS NOT NULL
          AND noise_raw IS NOT NULL
        """,
        (photo_id, algorithm_version, sha256),
    ).fetchone()
    return row is not None


def upsert_quality(
    conn: sqlite3.Connection,
    photo_id: int,
    raw: dict[str, float],
    scores: dict[str, float],
    quality_score: float | None,
    algorithm_version: str,
    sha256: str,
):
    """Insert/replace the quality row. ``quality_score`` stays NULL until the
    aggregation formula is frozen (M2 decision: do not write an unfrozen
    interpretation)."""
    conn.execute(
        """
        INSERT INTO quality
          (photo_id, sha256,
           sharpness_raw, exposure_raw, noise_raw,
           sharpness, exposure, noise,
           quality_score, algorithm_version, computed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(photo_id) DO UPDATE SET
          sha256 = excluded.sha256,
          sharpness_raw = excluded.sharpness_raw,
          exposure_raw = excluded.exposure_raw,
          noise_raw = excluded.noise_raw,
          sharpness = excluded.sharpness,
          exposure = excluded.exposure,
          noise = excluded.noise,
          quality_score = excluded.quality_score,
          algorithm_version = excluded.algorithm_version,
          computed_at = excluded.computed_at
        """,
        (
            photo_id,
            sha256,
            raw["sharpness_raw"],
            raw["exposure_raw"],
            raw["noise_raw"],
            scores["sharpness"],
            scores["exposure"],
            scores["noise"],
            quality_score,
            algorithm_version,
            _now(),
        ),
    )


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


# ---------------------------------------------------------------------------
# Preference / Creative Search reservation (schema v4, IDEA.md §22)
#
# Structure-only for now: no pipeline stage writes these tables yet. The
# functions exist so M3.1+ (Taste Profile, Intent) and the future Creative
# Search engine can use them without another schema migration.
# ---------------------------------------------------------------------------

# Valid values for preference_samples.source (IDEA.md §7 data flywheel)
PREFERENCE_SOURCES = ("user", "model", "vlm", "inference")


def upsert_preference_sample(
    conn: sqlite3.Connection,
    *,
    candidate_a: int | None,
    candidate_b: int | None,
    winner: int | None,
    source: str = "user",
    context: str | None = None,
    confidence: float | None = None,
    reason: str | None = None,
    user_id: str = "local",
) -> int:
    """Record one preference signal. ``winner`` may be NULL (un-decided
    sample). ``sample_id`` is stable for re-deciding the same sample via
    (candidate_a, candidate_b, user_id)."""
    if source not in PREFERENCE_SOURCES:
        raise ValueError(f"unknown preference source: {source!r}")
    existing = conn.execute(
        """
        SELECT sample_id FROM preference_samples
        WHERE candidate_a = ? AND candidate_b = ? AND user_id = ?
        """,
        (candidate_a, candidate_b, user_id),
    ).fetchone()
    if existing:
        sid = existing["sample_id"]
        conn.execute(
            """
            UPDATE preference_samples
            SET winner = ?, source = ?, context = ?, confidence = ?,
                reason = ?, timestamp = ?
            WHERE sample_id = ?
            """,
            (winner, source, context, confidence, reason, _now(), sid),
        )
        return sid
    cur = conn.execute(
        """
        INSERT INTO preference_samples
          (candidate_a, candidate_b, winner, context, source, confidence,
           reason, user_id, timestamp)
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (candidate_a, candidate_b, winner, context, source, confidence,
         reason, user_id, _now()),
    )
    return cur.lastrowid


def list_preference_samples(
    conn: sqlite3.Connection,
    *,
    user_id: str | None = None,
    source: str | None = None,
    winner: int | None = None,
    undecided_only: bool = False,
) -> list:
    """Read preference samples (filterable by user/source/winner)."""
    clauses, params = [], []
    if user_id is not None:
        clauses.append("user_id = ?"); params.append(user_id)
    if source is not None:
        clauses.append("source = ?"); params.append(source)
    if winner is not None:
        clauses.append("winner = ?"); params.append(winner)
    if undecided_only:
        clauses.append("winner IS NULL")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return conn.execute(
        f"SELECT * FROM preference_samples{where} ORDER BY timestamp",
        params,
    ).fetchall()


def add_creative_candidate(
    conn: sqlite3.Connection,
    *,
    prompt: str | None = None,
    seed: int | None = None,
    model: str | None = None,
    parameters: str | None = None,
    aesthetic_score: float | None = None,
    alignment_score: float | None = None,
    artifact_score: float | None = None,
    human_selection: int | None = None,
    rarity: str | None = None,
    tier: str | None = None,
) -> int:
    """Record one generated/sourced creative candidate."""
    cur = conn.execute(
        """
        INSERT INTO creative_candidates
          (prompt, seed, model, parameters,
           aesthetic_score, alignment_score, artifact_score,
           human_selection, rarity, tier, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (prompt, seed, model, parameters,
         aesthetic_score, alignment_score, artifact_score,
         human_selection, rarity, tier, _now()),
    )
    return cur.lastrowid


def list_creative_candidates(
    conn: sqlite3.Connection,
    *,
    model: str | None = None,
    human_selected_only: bool = False,
) -> list:
    """Read creative candidates (filterable by model / human selection)."""
    clauses, params = [], []
    if model is not None:
        clauses.append("model = ?"); params.append(model)
    if human_selected_only:
        clauses.append("human_selection IS NOT NULL")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return conn.execute(
        f"SELECT * FROM creative_candidates{where} ORDER BY candidate_id",
        params,
    ).fetchall()
