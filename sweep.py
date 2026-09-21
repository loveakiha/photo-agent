"""M1 threshold sweep: read-only parameter search over the near rule.

The sweep tries a grid of (pHash threshold, dHash threshold) combinations
against the hashes already in the database and reports, for each combo,
how many near pairs / groups / covered photos it produces. It is PURELY
READ-ONLY: it never writes to ``groups`` / ``decisions`` / any table, so
the database keeps representing only the currently-effective near config
while the sweep's results are just experiment output (Markdown under
``reports/sweep/<date>/``).

The pairing logic is shared with the official near pass (``near``): each
combo runs the exact same O(N^2) early-exit comparison on the pre-parsed
64-bit int hashes, so a combo's result is bit-exactly what ``near --phash
X --dhash Y`` would produce. The only thing the sweep changes is that it
does not commit groups to the database.
"""
from __future__ import annotations

from near import _fetch_rows, _pair_clusters
from hashes import HASH_VERSION

# Default 25-combo grid: pHash in {4,6,8,10,12} x dHash in {8,10,12,14,16}.
DEFAULT_PHASH_GRID = (4, 6, 8, 10, 12)
DEFAULT_DHASH_GRID = (8, 10, 12, 14, 16)


def run_sweep(
    conn,
    phash_grid=DEFAULT_PHASH_GRID,
    dhash_grid=DEFAULT_DHASH_GRID,
    algorithm_version: str | None = None,
) -> dict:
    """Run the read-only sweep.

    Returns ``{"photos": N, "results": [...]}`` where each result is a dict
    with phash, dhash, pairs, groups, members, coverage (0.0-1.0). Rows are
    fetched and parsed once; every combo reuses the same int arrays.
    """
    if algorithm_version is None:
        algorithm_version = HASH_VERSION

    rows = _fetch_rows(conn, algorithm_version)
    n = len(rows)
    phash_ints = [int(row["phash"], 16) for row in rows]
    dhash_ints = [int(row["dhash"], 16) for row in rows]

    results = []
    for ph in phash_grid:
        for dh in dhash_grid:
            pairs, clusters = _pair_clusters(
                rows, phash_ints, dhash_ints, ph, dh
            )
            groups = 0
            members = 0
            for member_list in clusters.values():
                if len(member_list) >= 2:
                    groups += 1
                    members += len(member_list)
            results.append(
                {
                    "phash": ph,
                    "dhash": dh,
                    "pairs": pairs,
                    "groups": groups,
                    "members": members,
                    "coverage": (members / n) if n else 0.0,
                }
            )
    return {"photos": n, "results": results}


def render_summary_md(
    photos: int,
    results: list[dict],
    phash_grid,
    dhash_grid,
) -> str:
    """Render the sweep results as a Markdown summary table.

    One row per combo, ordered by (phash, dhash) — the same order the
    sweep ran them in. ``coverage`` is rendered as a percentage.
    """
    header = (
        "| pHash | dHash | 对数 | 组数 | 成员 | 覆盖率 |"
    )
    sep = "|:---:|:---:|---:|---:|---:|---:|"
    lines = [
        f"# 阈值扫参 summary（{photos} 张）",
        "",
        "只读实验，未写入数据库。规则：pHash 距离 <= X 且 dHash 距离 <= Y 的",
        "候选对按连通分量合并成组。",
        "",
        header,
        sep,
    ]
    for r in results:
        cov = f"{r['coverage'] * 100:.1f}%"
        lines.append(
            f"| {r['phash']} | {r['dhash']} "
            f"| {r['pairs']} | {r['groups']} "
            f"| {r['members']} | {cov} |"
        )
    lines.append("")
    lines.append(
        f"网格：pHash ∈ {{{', '.join(map(str, phash_grid))}}}，"
        f"dHash ∈ {{{', '.join(map(str, dhash_grid))}}}，共 "
        f"{len(phash_grid) * len(dhash_grid)} 组。"
    )
    lines.append("")
    lines.append(
        "下一步：从上表挑 2-3 个候选组合，用 "
        "`cli.py sweep --sheets <combo>[,<combo>...]`（combo 形如 "
        "`phash8_dhash12`）生成 contact sheet，人工看误报/漏报后定稿阈值。"
    )
    lines.append("")
    return "\n".join(lines)
