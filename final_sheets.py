"""Regenerate the FINAL top5 contact sheets (post human-review) without
side effects (make_sheets.py top-level ran on import)."""
import math
import sqlite3
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

DB = "work/experiments/semantic_test.db"
THUMBS = Path("work/experiments/semantic_work/thumbs")
OUT = Path("work/experiments/semantic_reports/sheets")

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

finals = {
    "风景": ["DSC00506.JPG", "DSC00619.JPG", "DSC00702.JPG", "DSC00712.JPG", "DSC00746.JPG"],
    "人像": ["DSC00658.JPG", "DSC00660.JPG", "DSC00761.JPG", "DSC01403.JPG", "DSC01410.JPG"],
}

font = None
for cand in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyh.ttf"):
    try:
        font = ImageFont.truetype(cand, 14)
        break
    except Exception:
        pass
font = font or ImageFont.load_default()


def fit(draw, text, max_width):
    if draw.textlength(text, font=font) <= max_width:
        return text
    while len(text) > 1 and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return text + "…"


cell = 256
label_h = 34
for scene, rels in finals.items():
    n = len(rels)
    cols = 5
    rows_ = math.ceil(n / cols)
    canvas = Image.new("RGB", (cols * cell, rows_ * (cell + label_h)), "white")
    draw = ImageDraw.Draw(canvas)
    for i, rel in enumerate(rels):
        r = conn.execute(
            """SELECT p.sha256, sa.semantic_score FROM semantic_analysis sa
               JOIN photos p ON p.photo_id=sa.photo_id WHERE p.rel_path=?""",
            (rel,),
        ).fetchone()
        row_, col_ = divmod(i, cols)
        x0, y0 = col_ * cell, row_ * (cell + label_h)
        x1, y1 = x0 + cell, y0 + cell
        thumb = THUMBS / f"{r['sha256']}.jpg"
        draw.rectangle((x0, y0, x1, y1), fill="white")
        if thumb.exists():
            with Image.open(thumb) as t:
                img = t.copy().convert("RGB")
                img.thumbnail((cell, cell))
                canvas.paste(img, (x0 + (cell - img.width) // 2,
                                   y0 + (cell - img.height) // 2))
        draw.text((x0 + 4, y1 + 3),
                  fit(draw, f"{i+1}. {rel.split('/')[-1]} ({r['semantic_score']})", cell - 8),
                  fill="black", font=font)
    out = OUT / f"final_top5_{scene}.jpg"
    canvas.save(out, "JPEG", quality=88)
    print("wrote", out)
