"""Contact sheets: one contact image per duplicate group.

Each group (exact or near) becomes a single JPEG in which every member's
thumbnail is laid out in a grid, labeled with the file name. The sheets
are written under ``reports/contact/`` and referenced from the Markdown
report, so groups can be inspected at a glance without opening files.

Originals are never touched; only existing thumbnails (``work/thumbs``)
are composited. Missing thumbnails get a gray placeholder cell.
"""
from __future__ import annotations

import math
from pathlib import Path

# Windows Chinese-capable fonts, in preference order.
_LABEL_FONTS = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyh.ttf",
    "C:/Windows/Fonts/simhei.ttf",
)


def _load_font(size: int):
    from PIL import ImageFont

    for candidate in _LABEL_FONTS:
        try:
            return ImageFont.truetype(candidate, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _fit_label(draw, text: str, font, max_width: int) -> str:
    """Truncate a label (with ellipsis) to fit max_width pixels."""
    if draw.textlength(text, font=font) <= max_width:
        return text
    while len(text) > 1:
        text = text[:-1]
        if draw.textlength(text + "…", font=font) <= max_width:
            return text + "…"
    return text


def build_sheets_for_groups(
    member_lists: list[list],
    thumbs_dir: Path,
    out_dir: Path,
    name_prefix: str,
    cell_size: int = 256,
    max_cols: int = 4,
) -> list[Path]:
    """Render one contact sheet per in-memory member list.

    Each member list holds dicts with at least ``photo_id`` and
    ``rel_path``. Used by the read-only sweep (``sweep --sheets``) to show
    a candidate combination's groups without touching the database. Sheets
    are named ``{name_prefix}_G001.jpg`` ... in order.
    """
    from PIL import Image, ImageDraw

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    thumbs_dir = Path(thumbs_dir)
    label_h = max(16, cell_size // 12)
    font = _load_font(max(12, cell_size // 16))
    written: list[Path] = []
    for idx, members in enumerate(member_lists):
        if not members:
            continue
        sheet = _render_sheet(members, thumbs_dir, out_dir / f"{name_prefix}_G{idx + 1:03d}.jpg",
                              cell_size, max_cols, label_h, font, Image, ImageDraw)
        if sheet is not None:
            written.append(sheet)
    return written


def _render_sheet(members, thumbs_dir, out_path, cell_size, max_cols,
                  label_h, font, Image, ImageDraw):
    """Lay one member list out as a single JPEG grid; returns the path or
    None if the list is empty."""
    if not members:
        return None
    n = len(members)
    cols = min(max_cols, max(1, math.ceil(math.sqrt(n))))
    rows = math.ceil(n / cols)
    canvas = Image.new("RGB", (cols * cell_size, rows * (cell_size + label_h)), "white")
    draw = ImageDraw.Draw(canvas)

    for idx, member in enumerate(members):
        r, c = divmod(idx, cols)
        x0 = c * cell_size
        y0 = r * (cell_size + label_h)
        x1 = x0 + cell_size
        y1 = y0 + cell_size

        img = None
        # Keyed by SHA-256 (content-stable), matching scanner.py. A member
        # row may lack sha256 (shouldn't happen for real groups); fall back
        # to a placeholder in that case. Accepts dict or sqlite3.Row.
        sha = member["sha256"] if "sha256" in member.keys() else None
        thumb = thumbs_dir / f"{sha}.jpg" if sha else None
        if thumb is not None and thumb.exists():
            try:
                with Image.open(thumb) as t:
                    t = t.copy()
                    t.thumbnail((cell_size, cell_size))
                    img = t.convert("RGB")
            except Exception:
                img = None

        if img is None:
            draw.rectangle((x0, y0, x1, y1), fill=(215, 215, 215))
        else:
            draw.rectangle((x0, y0, x1, y1), fill="white")
            canvas.paste(
                img,
                (x0 + (cell_size - img.width) // 2,
                 y0 + (cell_size - img.height) // 2),
            )

        label = _fit_label(draw, Path(member["rel_path"]).name, font, cell_size - 8)
        draw.text((x0 + 4, y1 + 2), label, fill="black", font=font)

    canvas.save(out_path, "JPEG", quality=88)
    return out_path


def build_contact_sheets(
    conn,
    thumbs_dir: Path,
    out_dir: Path,
    kind: str = "near",
    cell_size: int = 256,
    max_cols: int = 4,
) -> list[Path]:
    """Compose one contact sheet per group of the given kind(s).

    ``kind`` is 'near', 'exact', or 'all'. Returns the list of written
    sheet paths (empty when there are no groups).
    """
    from PIL import Image, ImageDraw

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    thumbs_dir = Path(thumbs_dir)
    label_h = max(16, cell_size // 12)
    font = _load_font(max(12, cell_size // 16))

    kinds = ("near", "exact") if kind == "all" else (kind,)
    written: list[Path] = []

    for gkind in kinds:
        for group in conn.execute(
            "SELECT group_id FROM groups WHERE kind=? ORDER BY group_id", (gkind,)
        ):
            members = conn.execute(
                """
                SELECT p.photo_id, p.rel_path, p.sha256 FROM group_members gm
                JOIN photos p ON p.photo_id = gm.photo_id
                WHERE gm.group_id=? ORDER BY p.rel_path
                """,
                (group["group_id"],),
            ).fetchall()
            if not members:
                continue
            sheet = _render_sheet(
                members,
                thumbs_dir,
                out_dir / f"{gkind}_G{group['group_id']:03d}.jpg",
                cell_size,
                max_cols,
                label_h,
                font,
                Image,
                ImageDraw,
            )
            if sheet is not None:
                written.append(sheet)

    return written
