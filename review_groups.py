"""Find near-duplicate groups that overlap the AI top-5 lists, and render
comparison sheets (group members vs the AI pick) for human review.
"""
import sqlite3
from pathlib import Path

from contact_sheets import build_sheets_for_groups

DB = "work/experiments/semantic_test.db"
THUMBS = Path("work/experiments/semantic_work/thumbs")
OUT = Path("work/experiments/semantic_reports/sheets")
OUT.mkdir(parents=True, exist_ok=True)

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

top5 = {}
for scene in ("风景", "人像"):
    top5[scene] = {
        r["photo_id"]
        for r in conn.execute(
            """SELECT p.photo_id FROM semantic_analysis sa
               JOIN photos p ON p.photo_id=sa.photo_id
               WHERE sa.scene=? AND sa.semantic_score IS NOT NULL
               ORDER BY sa.semantic_score DESC, p.rel_path LIMIT 5""",
            (scene,),
        )
    }
print("top5 ids:", top5)

# Groups containing >=2 top-5 members -> these need human tie-breaking.
groups = conn.execute(
    """
    SELECT gm.group_id, g.kind, COUNT(*) AS n
    FROM group_members gm JOIN groups g ON g.group_id=gm.group_id
    WHERE gm.photo_id IN (SELECT photo_id FROM photos)
    GROUP BY gm.group_id
    """
).fetchall()

hits = []
for g in groups:
    members = conn.execute(
        "SELECT p.photo_id, p.rel_path, p.sha256 FROM group_members gm "
        "JOIN photos p ON p.photo_id=gm.photo_id WHERE gm.group_id=?",
        (g["group_id"],),
    ).fetchall()
    ids = {m["photo_id"] for m in members}
    overlap_l = ids & top5["风景"]
    overlap_p = ids & top5["人像"]
    if len(overlap_l) >= 2 or len(overlap_p) >= 2:
        hits.append((g, list(members)))
        print(f"G{g['group_id']} ({g['kind']}, n={g['n']}): "
              f"风景-overlap={len(overlap_l)} 人像-overlap={len(overlap_p)} -> "
              + ", ".join(m['rel_path'].split('/')[-1] for m in members))

if hits:
    sheets = build_sheets_for_groups(
        [m for _, m in hits], THUMBS, OUT, "review", cell_size=256, max_cols=4
    )
    for s in sheets:
        print("sheet:", s)
else:
    print("no near-dup group inside top5 (AI top-5 are all distinct) -> no human tie-break needed")
