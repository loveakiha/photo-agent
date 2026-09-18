"""M0 Markdown report: scan stats and exact duplicate groups."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path


def write_report(conn, reports_dir: Path, stats: dict | None = None) -> Path:
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    out = reports_dir / f"report_{now:%Y%m%d_%H%M%S}.md"

    total = conn.execute("SELECT COUNT(*) AS c FROM photos").fetchone()["c"]
    lines = [
        "# photo-agent 扫描报告（M0）",
        "",
        f"- 生成时间：{now:%Y-%m-%d %H:%M:%S}",
        f"- 数据库照片记录总数：{total}",
    ]

    if stats:
        lines.extend(
            [
                f"- 本次扫描：{stats.get('total', 0)} 个文件 "
                f"（新增 {stats.get('inserted', 0)}，更新 {stats.get('updated', 0)}）",
                f"- 缩略图：成功 {stats.get('thumb_ok', 0)}，失败 {stats.get('thumb_fail', 0)}",
            ]
        )

    lines.extend(["", "## 格式分布", "", "| 格式 | 数量 |", "|---|---:|"])
    for row in conn.execute(
        "SELECT COALESCE(format, '(未知)') AS f, COUNT(*) AS c "
        "FROM photos GROUP BY f ORDER BY c DESC"
    ):
        lines.append(f"| {row['f']} | {row['c']} |")

    group_count = conn.execute(
        "SELECT COUNT(*) AS c FROM groups WHERE kind='exact'"
    ).fetchone()["c"]
    duplicate_count = conn.execute(
        "SELECT COUNT(*) AS c FROM decisions WHERE status='DUPLICATE'"
    ).fetchone()["c"]

    lines.extend(
        [
            "",
            "## 精确重复（SHA-256 完全相同）",
            "",
            f"- 重复组数：**{group_count}**",
            f"- 标记 DUPLICATE 的文件数：**{duplicate_count}**（V1 只标记，不删除）",
        ]
    )

    for group in conn.execute(
        "SELECT * FROM groups WHERE kind='exact' ORDER BY group_id"
    ):
        rep = conn.execute(
            "SELECT rel_path FROM photos WHERE photo_id=?",
            (group["rep_photo_id"],),
        ).fetchone()["rel_path"]
        members = [
            row["rel_path"]
            for row in conn.execute(
                "SELECT p.rel_path FROM group_members gm "
                "JOIN photos p ON p.photo_id = gm.photo_id "
                "WHERE gm.group_id=? ORDER BY p.rel_path",
                (group["group_id"],),
            )
        ]
        lines.extend(
            [
                "",
                f"### 组 G{group['group_id']:03d}（{len(members)} 张）",
                f"- ★ 代表（保留）：`{rep}`",
            ]
        )
        for member in members:
            if member != rep:
                lines.append(f"- 重复：`{member}`")

    lines.extend(["", "---", "*photo-agent M0 · V1 不删除/不移动原图*"])
    text = "\n".join(lines)
    out.write_text(text, encoding="utf-8")
    (reports_dir / "latest.md").write_text(text, encoding="utf-8")
    return out
