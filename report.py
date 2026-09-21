"""M0 Markdown report: scan stats, duplicate groups, contact sheets."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from similarity import hamming_distance


def write_report(
    conn,
    reports_dir: Path,
    stats: dict | None = None,
    near_result: dict | None = None,
    thumbs_dir: Path | None = None,
) -> Path:
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    out = reports_dir / f"report_{now:%Y%m%d_%H%M%S}.md"

    # One contact sheet per group (embedded below each group when thumbs
    # are available). Sheets live in reports/contact/, relative to the
    # report file itself.
    sheets: dict[str, dict[int, Path]] = {"exact": {}, "near": {}}
    if thumbs_dir is not None:
        from contact_sheets import build_contact_sheets

        written = build_contact_sheets(conn, thumbs_dir, reports_dir / "contact", kind="all")
        for path in written:
            prefix, gid = path.stem.split("_G")
            gid = int(gid)
            sheets[prefix][gid] = path

    total = conn.execute("SELECT COUNT(*) AS c FROM photos").fetchone()["c"]
    lines = [
        "# photo-agent 扫描报告（M1）",
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
        sheet = sheets["exact"].get(group["group_id"])
        if sheet is not None:
            lines.append(f"![组 G{group['group_id']:03d} 拼图](contact/{sheet.name})")

    near_group_count = conn.execute(
        "SELECT COUNT(*) AS c FROM groups WHERE kind='near'"
    ).fetchone()["c"]

    lines.extend(
        [
            "",
            "## 近似重复（M1 · pHash/dHash 候选，未标记）",
            "",
            f"- 候选组数：**{near_group_count}**"
            + (
                f"（本次运行：{near_result['near_pairs']} 对，"
                f"{near_result['near_members']} 个非代表成员）"
                if near_result
                else ""
            ),
            "- 判据：pHash 距离 ≤ {p} 且 dHash 距离 ≤ {d}（CLI `near` 的 "
            "`--phash/--dhash` 可覆盖）".format(
                p=near_result["phash_threshold"] if near_result else 8,
                d=near_result["dhash_threshold"] if near_result else 12,
            ),
            "- 语义：组是**连通分量候选组**——只保证「相邻成员满足阈值」，"
            "不保证组内任意两张都满足阈值",
            "- 与精确重复的关系：SHA-256 相同（字节级完全一致）的配对只出现在"
            "「精确重复」一节，本节只包含文件不同但视觉相似的候选",
            "- 说明：仅候选，不代表最终重复判定；阈值是工程参数，待真实照片库实验确定",
        ]
    )

    for group in conn.execute(
        "SELECT * FROM groups WHERE kind='near' ORDER BY group_id"
    ):
        rep = conn.execute(
            "SELECT rel_path FROM photos WHERE photo_id=?",
            (group["rep_photo_id"],),
        ).fetchone()["rel_path"]
        rep_hashes = conn.execute(
            "SELECT phash, dhash FROM photo_hashes WHERE photo_id=?",
            (group["rep_photo_id"],),
        ).fetchone()
        members = [
            row
            for row in conn.execute(
                """
                SELECT p.rel_path, ph.phash, ph.dhash
                FROM group_members gm
                JOIN photos p ON p.photo_id = gm.photo_id
                JOIN photo_hashes ph ON ph.photo_id = gm.photo_id
                WHERE gm.group_id=? ORDER BY p.rel_path
                """,
                (group["group_id"],),
            )
        ]
        lines.extend(
            [
                "",
                f"### 组 G{group['group_id']:03d}（{len(members)} 张）",
                f"- ★ 代表：`{rep}`",
            ]
        )
        for row in members:
            if row["rel_path"] != rep:
                if rep_hashes is not None and row["phash"] is not None:
                    deltas = (
                        f"（pHash Δ {hamming_distance(rep_hashes['phash'], row['phash'])}"
                        f" · dHash Δ {hamming_distance(rep_hashes['dhash'], row['dhash'])}）"
                    )
                else:
                    deltas = ""
                lines.append(f"- 候选：`{row['rel_path']}`{deltas}")
        sheet = sheets["near"].get(group["group_id"])
        if sheet is not None:
            lines.append(f"![组 G{group['group_id']:03d} 拼图](contact/{sheet.name})")

    lines.extend(_quality_section(conn, reports_dir, thumbs_dir))

    lines.extend(["", "---", "*photo-agent M2 · V1 不删除/不移动原图*"])
    text = "\n".join(lines)
    out.write_text(text, encoding="utf-8")
    (reports_dir / "latest.md").write_text(text, encoding="utf-8")
    return out


def _thumb_link(reports_dir: Path, thumbs_dir: Path | None, sha: str | None,
                rel_path: str) -> str:
    """Embed a photo's thumbnail in the report, or fall back to a plain
    cell. Thumbnails are SHA-256-keyed in work/thumbs (same stable-identity
    convention as M0/M1); they are copied into reports/quality/ so the
    report folder stays self-contained (like reports/contact/)."""
    if not thumbs_dir or not sha:
        return ""
    src = Path(thumbs_dir) / f"{sha}.jpg"
    if not src.is_file():
        return ""
    dst_dir = reports_dir / "quality"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / f"{sha}.jpg"
    if not dst.exists():
        dst.write_bytes(src.read_bytes())
    return f"![{rel_path}](quality/{dst.name})"


def _quality_section(conn, reports_dir: Path, thumbs_dir: Path | None) -> list:
    """M2 quality section: distributions + worst/best Top 5 boards.

    ``quality_score`` is intentionally absent — the aggregation formula is
    not frozen yet, so the report shows only the measured dimensions.
    Each board row embeds its thumbnail (copied from the SHA-256-keyed
    work/thumbs into reports/quality/, same convention as contact/).
    """
    rows = conn.execute(
        """
        SELECT q.*, p.rel_path, p.sha256
        FROM quality q JOIN photos p ON p.photo_id = q.photo_id
        """
    ).fetchall()
    if not rows:
        return [
            "",
            "## 质量评分（M2 · 尚未计算，运行 `quality` 命令）",
        ]

    def stats(name: str) -> str:
        values = sorted(r[name] for r in rows if r[name] is not None)
        n = len(values)
        if not n:
            return "(无)"
        return (
            f"min {values[0]:.3g} / p10 {values[min(n - 1, max(0, int(n * 0.1)) - 1)]:.3g} "
            f"/ median {values[n // 2]:.3g} / p90 {values[min(n - 1, int(n * 0.9))]:.3g} "
            f"/ max {values[-1]:.3g}"
        )

    version = rows[0]["algorithm_version"]
    dims = ("sharpness", "exposure", "noise")

    def min_dim(r):
        values = [r[d] for d in dims if r[d] is not None]
        return min(values) if values else None

    lines = [
        "",
        f"## 质量评分（M2 · Measurement · {version}）",
        "",
        f"- 已评分照片：**{len(rows)}**",
        "- 定位：只回答「是否存在**明显**技术问题（模糊/裁剪曝光/噪声）」；",
        "- 维度分数：0=明显问题端，1=良好端（两档线性钳制，阈值见 config.yaml）",
        f"- sharpness（Laplacian 方差，越高越好）：{stats('sharpness')}",
        f"- exposure（两端裁剪率，越低越好）：{stats('exposure')}",
        f"- noise（局部方差中位数，越低越好）：{stats('noise')}",
        "- quality_score 暂缓：聚合公式待实验数据冻结，期间此列留空；",
        "  排序暂用「最差维度分」（三维度取最低）作为临时综合信号",
    ]

    def board(title: str, note: str, entries):
        out = ["", f"### {title}", "", note, "",
               "| 缩略图 | 照片 | sharpness | exposure | noise | 最差维度 |",
               "|:---:|---|---:|---:|---:|---|"]
        if not entries:
            out.append("|  | （无） |  |  |  |  |")
        for rel_path, d, sha in entries:
            worst_name = min(dims, key=lambda k: d[k] if d[k] is not None else 1.0)
            img = _thumb_link(reports_dir, thumbs_dir, sha, rel_path)
            out.append(
                f"| {img} | `{rel_path}` | {d['sharpness']:.2f} | {d['exposure']:.2f} "
                f"| {d['noise']:.2f} | {worst_name} |"
            )
        return out

    ranked = [
        (min_dim(r), r["rel_path"], r["sha256"],
         {"sharpness": r["sharpness"], "exposure": r["exposure"], "noise": r["noise"]})
        for r in rows if min_dim(r) is not None
    ]
    ranked.sort()
    worst5 = [(rel, d, sha) for _, rel, sha, d in ranked[:5]]
    best5 = [(rel, d, sha) for _, rel, sha, d in reversed(ranked[-5:])]

    lines.extend(board(
        "最差 Top 5（技术风险最高）",
        "按最差维度分升序——任一维度明显偏低的问题照片",
        worst5,
    ))
    lines.extend(board(
        "最好 Top 5（技术质量最稳）",
        "按最差维度分降序——三维度都尽量高的照片",
        best5,
    ))
    return lines
