"""Render contact sheets for the 3 semantic tests (top5 landscape/portrait,
shrimp candidates) so the user can review them in one image each."""
import math
import sqlite3
from pathlib import Path

DB = "work/experiments/semantic_test.db"
THUMBS = Path("work/experiments/semantic_work/thumbs")
OUT = Path("work/experiments/semantic_reports/sheets")
OUT.mkdir(parents=True, exist_ok=True)

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row


def _fit(draw, text, font, max_width):
    if draw.textlength(text, font=font) <= max_width:
        return text
    while len(text) > 1 and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return text + "…"


def _labeled_sheet(members, thumbs_dir, out_path, cell_size=256, max_cols=5):
    from PIL import Image, ImageDraw, ImageFont

    font = None
    for cand in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyh.ttf"):
        try:
            font = ImageFont.truetype(cand, 14)
            break
        except Exception:
            pass
    font = font or ImageFont.load_default()
    label_h = 34
    n = len(members)
    cols = min(max_cols, max(1, math.ceil(math.sqrt(n))))
    rowc = math.ceil(n / cols)
    canvas = Image.new("RGB", (cols * cell_size, rowc * (cell_size + label_h)), "white")
    draw = ImageDraw.Draw(canvas)
    for idx, member in enumerate(members):
        r, c = divmod(idx, cols)
        x0 = c * cell_size
        y0 = r * (cell_size + label_h)
        x1 = x0 + cell_size
        y1 = y0 + cell_size
        sha = member.get("sha256")
        thumb = thumbs_dir / f"{sha}.jpg" if sha else None
        img = None
        if thumb is not None and thumb.exists():
            try:
                with Image.open(thumb) as t:
                    img = t.copy().convert("RGB")
                    img.thumbnail((cell_size, cell_size))
            except Exception:
                img = None
        if img is None:
            draw.rectangle((x0, y0, x1, y1), fill=(215, 215, 215))
        else:
            draw.rectangle((x0, y0, x1, y1), fill="white")
            canvas.paste(img, (x0 + (cell_size - img.width) // 2,
                               y0 + (cell_size - img.height) // 2))
        label = _fit(draw, member.get("label") or Path(member["rel_path"]).name,
                     font, cell_size - 8)
        draw.text((x0 + 4, y1 + 3), label, fill="black", font=font)
    canvas.save(out_path, "JPEG", quality=88)
    return out_path


def rows(scene, k=5):
    return conn.execute(
        """
        SELECT sa.photo_id, p.rel_path, p.sha256, sa.semantic_score,
               sa.subjects, sa.context
        FROM semantic_analysis sa JOIN photos p ON p.photo_id=sa.photo_id
        WHERE sa.scene = ? AND sa.semantic_score IS NOT NULL
        ORDER BY sa.semantic_score DESC, p.rel_path LIMIT ?
        """,
        (scene, k),
    ).fetchall()


for scene in ("风景", "人像"):
    members = []
    for r in rows(scene, 5):
        m = dict(r)
        m["label"] = f"{r['rel_path'].split('/')[-1]} (score {r['semantic_score']})"
        members.append(m)
    path = _labeled_sheet(members, THUMBS, OUT / f"top5_{scene}.jpg")
    print("wrote", path)

shrimp = conn.execute(
    """
    SELECT sa.photo_id, p.rel_path, p.sha256, sa.semantic_score,
           sa.subjects, sa.context, sa.scene
    FROM semantic_analysis sa JOIN photos p ON p.photo_id=sa.photo_id
    WHERE sa.semantic_score IS NOT NULL
      AND (sa.subjects LIKE '%虾%' OR sa.context LIKE '%虾%')
    ORDER BY sa.semantic_score DESC, p.rel_path
    """
).fetchall()
print("shrimp candidates:", len(shrimp))
for r in shrimp:
    print("  ", r["rel_path"], "|", r["scene"], "|", r["semantic_score"],
          "|", r["subjects"], "|", r["context"])
members = []
for r in shrimp[:9]:
    m = dict(r)
    m["label"] = (f"{r['rel_path'].split('/')[-1]} ({r['semantic_score']}) "
                  f"{r['subjects']}")
    members.append(m)
if members:
    path = _labeled_sheet(members, THUMBS, OUT / "shrimp_candidates.jpg")
    print("wrote", path)
