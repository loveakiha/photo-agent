"""photo-agent CLI for M0."""
from __future__ import annotations

from pathlib import Path

import typer
import yaml

from database import connect
from doctor import run_checks
from duplicate import build_exact_groups
from report import write_report
from scanner import scan as run_scan

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
        f"更新 {stats.get('updated', 0)} | "
        f"缩略图失败 {stats.get('thumb_fail', 0)}"
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
    """生成 Markdown 报告。"""
    cfg = load_cfg()
    conn = connect(db_path(cfg))
    try:
        output = write_report(conn, reports_dir(cfg))
    finally:
        conn.close()
    print(f"报告已生成：{output}")


@app.command(name="all")
def run_all(
    dir: Path = typer.Option(..., "--dir", help="照片根目录"),
    limit: int = typer.Option(0, "--limit", help="只处理前 N 个文件（0=全部）"),
):
    """scan + dedup + report 一条龙。"""
    cfg = load_cfg()
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
        output = write_report(conn, reports_dir(cfg), stats=stats)
    finally:
        conn.close()

    print(
        f"扫描：{stats.get('total', 0)} 文件 "
        f"（新增 {stats.get('inserted', 0)}，更新 {stats.get('updated', 0)}）"
    )
    print(
        f"精确重复：{result['exact_groups']} 组，"
        f"{result['duplicates']} 个 DUPLICATE"
    )
    print(f"报告：{output}")


if __name__ == "__main__":
    app()
