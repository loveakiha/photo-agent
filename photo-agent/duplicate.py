"""Stage 1: exact deduplication using SHA-256.

Only groups and decisions are written. Originals are never deleted or moved.
"""
from __future__ import annotations

from collections import defaultdict

from database import add_group, add_group_member, clear_exact_results, set_decision


def _canonical_key(row):
    depth = row["rel_path"].count("/")
    return (depth, row["rel_path"], row["first_seen"] or "")


def build_exact_groups(conn) -> dict:
    clear_exact_results(conn)
    rows = conn.execute(
        "SELECT photo_id, rel_path, sha256, first_seen FROM photos ORDER BY photo_id"
    ).fetchall()

    by_sha: dict[str, list] = defaultdict(list)
    for row in rows:
        by_sha[row["sha256"]].append(row)

    n_groups = 0
    n_dups = 0

    for members in by_sha.values():
        if len(members) < 2:
            continue

        rep = min(members, key=_canonical_key)
        group_id = add_group(
            conn,
            kind="exact",
            rep_photo_id=rep["photo_id"],
            size=len(members),
        )

        for member in members:
            add_group_member(
                conn,
                group_id,
                member["photo_id"],
                recall_source="sha256",
                sim_to_rep=1.0,
            )
            if member["photo_id"] != rep["photo_id"]:
                set_decision(
                    conn,
                    member["photo_id"],
                    "DUPLICATE",
                    None,
                    f"与 {rep['rel_path']} 字节级相同 (SHA-256)",
                    "algo",
                    "v1",
                )
                n_dups += 1

        n_groups += 1

    conn.commit()
    return {"exact_groups": n_groups, "duplicates": n_dups}
