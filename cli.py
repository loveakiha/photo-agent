"""photo-agent CLI for M0."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import typer
import yaml

from contact_sheets import build_contact_sheets, build_sheets_for_groups
from database import connect
from doctor import run_checks
from duplicate import build_exact_groups
from near import build_near_groups, threshold_groups
from quality import DEFAULT_LONG_EDGE, DEFAULT_THRESHOLDS, QUALITY_VERSION, run_quality
from report import write_report
from scanner import scan as run_scan
from similarity import DEFAULT_PHASH_THRESHOLD, DEFAULT_DHASH_THRESHOLD
from sweep import DEFAULT_PHASH_GRID, DEFAULT_DHASH_GRID, render_summary_md, run_sweep

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="photo-agent：本地照片整理系统（V1 只分析，不碰原图）",
)


def load_cfg() -> dict:
    if not CONFIG_PATH.exists():
        typer.secho(
            f"缺少配置文件：{CONFIG_PATH}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1)
    with open(CONFIG_PATH, encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _abs(path_value) -> Path:
    path = Path(path_value)
    return path if path.is_absolute() else BASE_DIR / path


def db_path(cfg: dict) -> Path:
    return _abs((cfg.get("output") or {}).get("db", "photo.db"))


def thumbs_dir(cfg: dict) -> Path:
    return _abs((cfg.get("output") or {}).get("work_dir", "work")) / "thumbs"


def reports_dir(cfg: dict) -> Path:
    return _abs((cfg.get("output") or {}).get("reports_dir", "reports"))


def quality_settings(cfg: dict) -> tuple[int, dict]:
    """Effective (long_edge, thresholds) for quality computation: config
    ``quality.*`` wins, code defaults otherwise."""
    q = cfg.get("quality") or {}
    long_edge = int(q.get("long_edge", DEFAULT_LONG_EDGE))
    thresholds = q.get("thresholds") or DEFAULT_THRESHOLDS
    return long_edge, thresholds


def near_thresholds(cfg: dict) -> tuple[int, int]:
    """Effective (phash, dhash) defaults for this run.

    ``config.yaml``'s ``near.phash`` / ``near.dhash`` win when present;
    otherwise the code-level defaults apply. CLI ``--phash`` / ``--dhash``
    override whatever this returns.
    """
    near = cfg.get("near") or {}
    phash = near.get("phash", DEFAULT_PHASH_THRESHOLD)
    dhash = near.get("dhash", DEFAULT_DHASH_THRESHOLD)
    return int(phash), int(dhash)


def resolve_thresholds(
    cfg: dict,
    cli_phash: int | None = None,
    cli_dhash: int | None = None,
) -> tuple[int, int]:
    """Final (phash, dhash) for a run: explicit CLI value > config > code
    default. ``None`` means "not given on the command line"."""
    p, d = near_thresholds(cfg)
    if cli_phash is not None:
        p = cli_phash
    if cli_dhash is not None:
        d = cli_dhash
    return p, d


def _parse_combo(spec: str) -> tuple[int, int] | None:
    """Parse ``phash8_dhash12`` -> (8, 12); None if malformed.

    The format is ``phash<N1>_dhash<N2>``: the numbers trail the ``phash``
    and ``dhash`` keywords, so split on the literal ``_dhash`` join.
    """
    spec = spec.strip().lower()
    if not spec.startswith("phash"):
        return None
    rest = spec[len("phash"):]
    if "_dhash" not in rest:
        return None
    phash_part, dhash_part = rest.split("_dhash", 1)
    if not (phash_part.isdigit() and dhash_part.isdigit()):
        return None
    return int(phash_part), int(dhash_part)


def _require_dir(directory: Path) -> Path:
    directory = directory.resolve()
    if not directory.is_dir():
        typer.secho(
            f"目录不存在：{directory}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1)
    return directory


@app.command()
def doctor():
    """环境自检。"""
    print("photo-agent doctor")
    results = run_checks(load_cfg())
    for mark, message in results:
        print(f"  {mark} {message}")
    bad = sum(mark == "✗" for mark, _ in results)
    if bad:
        raise typer.Exit(1)
    print("\n全部通过，可以开工。")


@app.command()
def scan(
    dir: Path = typer.Option(..., "--dir", help="照片根目录"),
    limit: int = typer.Option(0, "--limit", help="只处理前 N 个文件（0=全部）"),
):
    """Stage 0：扫描 → SHA256 → 元数据 → 缩略图 → 入库。"""
    cfg = load_cfg()
    conn = connect(db_path(cfg))
    try:
        stats = run_scan(
            _require_dir(dir),
            cfg,
            conn,
            thumbs_dir(cfg),
            limit=limit,
        )
    finally:
        conn.close()
    print(
        f"扫描完成：{stats.get('total', 0)} 个文件 | "
        f"新增 {stats.get('inserted', 0)} "
        f"更新 {stats.get('updated', 0)} "
        f"移动合并 {stats.get('moved', 0)} "
        f"清理失效 {stats.get('pruned', 0)} | "
        f"hash {stats.get('hash_ok', 0)} 算/"
        f"{stats.get('hash_skipped', 0)} 缓存/"
        f"{stats.get('hash_fail', 0)} 失败 | "
        f"缩略图失败 {stats.get('thumb_fail', 0)}"
    )


@app.command()
def near(
    phash_threshold: int | None = typer.Option(None, "--phash", help="pHash 汉明距离阈值（默认取 config.yaml near.phash）"),
    dhash_threshold: int | None = typer.Option(None, "--dhash", help="dHash 汉明距离阈值（默认取 config.yaml near.dhash）"),
):
    """Stage 2（M1）：pHash/dHash 近似重复分组（只建候选组，不标记）。"""
    cfg = load_cfg()
    phash_threshold, dhash_threshold = resolve_thresholds(
        cfg, phash_threshold, dhash_threshold
    )
    conn = connect(db_path(cfg))
    try:
        result = build_near_groups(
            conn,
            phash_threshold=phash_threshold,
            dhash_threshold=dhash_threshold,
        )
    finally:
        conn.close()
    print(
        f"近似重复（候选）：{result['near_groups']} 组，"
        f"{result['near_pairs']} 对，{result['near_members']} 个非代表成员"
        f"（阈值 pHash<={phash_threshold} 且 dHash<={dhash_threshold}）"
    )


@app.command()
def quality(
    force: bool = typer.Option(
        False, "--force", help="忽略缓存，全量重算（默认增量：仅缺行/版本不符/sha256 变化时重算）"
    ),
):
    """Stage（M2）：技术质量测量（纯本地、CPU，不碰原图）。"""
    cfg = load_cfg()
    conn = connect(db_path(cfg))
    try:
        stats = run_quality(conn, cfg, force=force)
    finally:
        conn.close()
    print(
        f"质量测量（{QUALITY_VERSION}）：{stats.get('total', 0)} 张 | "
        f"新算 {stats.get('computed', 0)} / "
        f"缓存 {stats.get('cached', 0)} / "
        f"失败 {stats.get('failed', 0)}"
    )


@app.command()
def dedup():
    """Stage 1：SHA-256 精确去重（只建组 + 标记，不删除）。"""
    cfg = load_cfg()
    conn = connect(db_path(cfg))
    try:
        result = build_exact_groups(conn)
    finally:
        conn.close()
    print(
        f"精确重复：{result['exact_groups']} 组，"
        f"{result['duplicates']} 个文件标记 DUPLICATE（原图未动）"
    )


@app.command()
def report():
    """生成 Markdown 报告（含每组缩略图拼图）。"""
    cfg = load_cfg()
    conn = connect(db_path(cfg))
    try:
        output = write_report(conn, reports_dir(cfg), thumbs_dir=thumbs_dir(cfg))
    finally:
        conn.close()
    print(f"报告已生成：{output}")


@app.command()
def contact(
    kind: str = typer.Option("all", "--kind", help="拼图类型：near / exact / all"),
):
    """只重新生成同组缩略图拼图（不重新扫描/分组）。"""
    cfg = load_cfg()
    conn = connect(db_path(cfg))
    try:
        sheets = build_contact_sheets(
            conn, thumbs_dir(cfg), reports_dir(cfg) / "contact", kind=kind
        )
    finally:
        conn.close()
    if not sheets:
        print("当前没有可拼图的分组（先运行 all 或 near）")
        return
    print(f"已生成 {len(sheets)} 张拼图：{reports_dir(cfg) / 'contact'}")


@app.command()
def sweep(
    sheets: str = typer.Option(
        None,
        "--sheets",
        help="按需生成 contact sheet：逗号分隔的组合，如 phash8_dhash12,phash10_dhash14（只生成 size>=2 的组）",
    ),
):
    """M1 阈值扫参：只读实验，扫描 pHash x dHash 网格，输出 summary.md（不写数据库）。"""
    cfg = load_cfg()
    conn = connect(db_path(cfg))
    try:
        out = run_sweep(conn)
    finally:
        conn.close()

    out_dir = reports_dir(cfg) / "sweep" / datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    summary_path = out_dir / "summary.md"
    summary_path.write_text(
        render_summary_md(
            out["photos"], out["results"], DEFAULT_PHASH_GRID, DEFAULT_DHASH_GRID
        ),
        encoding="utf-8",
    )
    print(f"扫参完成：{out['photos']} 张，{len(out['results'])} 组组合（只读，未写库）")
    print(f"summary：{summary_path}")
    print("从 summary 挑 2-3 个组合后，用 --sheets <combo>[,<combo>...] 生成拼图。")

    if sheets:
        _render_sweep_sheets(cfg, sheets)


def _render_sweep_sheets(cfg: dict, sheets: str) -> None:
    """Render contact sheets for one or more candidate combos (read-only).

    ``sheets`` is a comma-separated list of ``phashX_dhashY`` specs. Each
    combo's groups (size >= 2 only) are rendered to
    ``reports/sweep/<combo>/`` without touching the database.
    """
    for spec in sheets.split(","):
        combo = _parse_combo(spec)
        if combo is None:
            typer.secho(
                f"忽略无法解析的组合：{spec.strip()}（应为 phashX_dhashY 形式）",
                fg=typer.colors.YELLOW,
                err=True,
            )
            continue
        phash, dhash = combo
        conn = connect(db_path(cfg))
        try:
            groups = threshold_groups(conn, phash, dhash)
        finally:
            conn.close()
        if not groups:
            print(f"{spec}：没有 size>=2 的组，跳过拼图。")
            continue
        combo_dir = reports_dir(cfg) / "sweep" / f"phash{phash}_dhash{dhash}"
        written = build_sheets_for_groups(
            groups,
            thumbs_dir(cfg),
            combo_dir,
            name_prefix=f"phash{phash}_dhash{dhash}",
        )
        print(f"{spec}：{len(written)} 张拼图 -> {combo_dir}")


@app.command(name="all")
def run_all(
    dir: Path = typer.Option(..., "--dir", help="照片根目录"),
    limit: int = typer.Option(0, "--limit", help="只处理前 N 个文件（0=全部）"),
    phash_threshold: int | None = typer.Option(None, "--phash", help="pHash 汉明距离阈值（默认取 config.yaml near.phash）"),
    dhash_threshold: int | None = typer.Option(None, "--dhash", help="dHash 汉明距离阈值（默认取 config.yaml near.dhash）"),
):
    """scan + dedup + near + quality + report 一条龙。"""
    cfg = load_cfg()
    phash_threshold, dhash_threshold = resolve_thresholds(
        cfg, phash_threshold, dhash_threshold
    )
    directory = _require_dir(dir)
    conn = connect(db_path(cfg))
    try:
        stats = run_scan(
            directory,
            cfg,
            conn,
            thumbs_dir(cfg),
            limit=limit,
        )
        result = build_exact_groups(conn)
        near_result = build_near_groups(
            conn,
            phash_threshold=phash_threshold,
            dhash_threshold=dhash_threshold,
        )
        quality_stats = run_quality(conn, cfg)
        output = write_report(
            conn,
            reports_dir(cfg),
            stats=stats,
            near_result=near_result,
            thumbs_dir=thumbs_dir(cfg),
        )
    finally:
        conn.close()

    print(
        f"扫描：{stats.get('total', 0)} 文件 "
        f"（新增 {stats.get('inserted', 0)}，更新 {stats.get('updated', 0)}，"
        f"移动合并 {stats.get('moved', 0)}，清理失效 {stats.get('pruned', 0)}）"
    )
    print(
        f"精确重复：{result['exact_groups']} 组，"
        f"{result['duplicates']} 个 DUPLICATE"
    )
    print(
        f"近似重复（候选）：{near_result['near_groups']} 组，"
        f"{near_result['near_pairs']} 对"
    )
    print(
        f"质量测量（{QUALITY_VERSION}）：新算 {quality_stats.get('computed', 0)} / "
        f"缓存 {quality_stats.get('cached', 0)} / "
        f"失败 {quality_stats.get('failed', 0)}"
    )
    print(f"报告：{output}")


if __name__ == "__main__":
    app()
