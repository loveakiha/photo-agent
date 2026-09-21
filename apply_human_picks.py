"""Apply human decisions from test-2 review, re-rank the full-set top5.

Rules:
- G17 (near, landscape): user picked DSC00506 -> DSC00505 drops out.
- G48 (near, portrait): user picked DSC00662 -> 663/664/665 drop out,
  662 enters (a near-dup group occupies at most one top5 slot).
- Vacated slots are filled by the next-highest semantic_score of that scene
  (photos in a near-dup group that are NOT the user/policy pick are
  excluded so one group cannot take multiple slots).
Decisions are written to the decisions table with source='user'.
"""
import sqlite3

DB = "work/experiments/semantic_test.db"
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

picks = {
    "风景": {17: "DSC00506.JPG"},   # group 17 -> winner rel_path
    "人像": {48: "DSC00662.JPG"},
}

# group_id -> member rel_paths; winner = user pick.
excluded: set[str] = set()
winners: set[str] = set()
for scene, groups in picks.items():
    for gid, winner_rel in groups.items():
        members = conn.execute(
            "SELECT p.rel_path FROM group_members gm "
            "JOIN photos p ON p.photo_id=gm.photo_id WHERE gm.group_id=?",
            (gid,),
        ).fetchall()
        rels = {m["rel_path"] for m in members}
        winners.add(winner_rel)
        excluded.update(rels - {winner_rel})

# Record user decisions on the excluded members (they lose to the group winner).
import datetime

now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
for rel in sorted(excluded):
    row = conn.execute(
        "SELECT photo_id FROM photos WHERE rel_path=?", (rel,)
    ).fetchone()
    if row:
        conn.execute(
            """
            INSERT INTO decisions (photo_id, status, final_score, reason,
                                   source, rule_version, updated_at)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT (photo_id) DO UPDATE SET
              status=excluded.status, final_score=excluded.final_score,
              reason=excluded.reason, source=excluded.source,
              rule_version=excluded.rule_version, updated_at=excluded.updated_at
            """,
            (
                row["photo_id"], "DISCARD", None,
                f"人工筛选：与赢家近重复，用户指定该组代表为 {winners and sorted(winners)[0]}",
                "user", "m3b-v1", now,
            ),
        )
# Winners get KEEP (policy pick in their group).
for rel in sorted(winners):
    row = conn.execute(
        "SELECT photo_id FROM photos WHERE rel_path=?", (rel,)
    ).fetchone()
    if row:
        conn.execute(
            """
            INSERT INTO decisions (photo_id, status, final_score, reason,
                                   source, rule_version, updated_at)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT (photo_id) DO UPDATE SET
              status=excluded.status, final_score=excluded.final_score,
              reason=excluded.reason, source=excluded.source,
              rule_version=excluded.rule_version, updated_at=excluded.updated_at
            """,
            (
                row["photo_id"], "KEEP", None,
                "人工筛选：用户在近重复组对比中选定此张为该组代表",
                "user", "m3b-v1", now,
            ),
        )
conn.commit()

print("excluded:", sorted(excluded))
print("winners :", sorted(winners))
print()

for scene in ("风景", "人像"):
    rows = conn.execute(
        """
        SELECT p.rel_path, sa.semantic_score, sa.subjects, sa.context
        FROM semantic_analysis sa JOIN photos p ON p.photo_id=sa.photo_id
        WHERE sa.scene=? AND sa.semantic_score IS NOT NULL
          AND p.rel_path NOT IN (%s)
        ORDER BY sa.semantic_score DESC, p.rel_path
        """ % ",".join("?" * len(excluded)),
        [scene] + sorted(excluded),
    ).fetchall()
    # A near-dup group may still contain several non-excluded members if only
    # one of them was the user's pick... keep only the first per group.
    seen_groups: set[int] = set()
    final = []
    for r in rows:
        gid = conn.execute(
            "SELECT gm.group_id FROM group_members gm "
            "JOIN photos p ON p.photo_id=gm.photo_id "
            "WHERE p.rel_path=? LIMIT 1",
            (r["rel_path"],),
        ).fetchone()
        if gid:
            if gid["group_id"] in seen_groups:
                continue
            seen_groups.add(gid["group_id"])
        final.append(r)
        if len(final) == 5:
            break
    print(f"===== 最终 {scene} TOP5（人工筛选后）=====")
    for i, r in enumerate(final, 1):
        print(f"{i}. {r['rel_path']}  score={r['semantic_score']}  {r['subjects']}")
    print()
