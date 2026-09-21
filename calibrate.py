"""M2 threshold calibration (one-off experiment, read-only on the library).

Computes the three raw quality indicators over the real library (all
photos currently in the database) plus the 18 synthetic fixtures, prints
raw distributions, verifies the fixture ordering, and writes a markdown
summary to reports/calibration/<timestamp>/ for the human freeze step.

Usage:
    .venv/Scripts/python calibrate.py
"""
from __future__ import annotations

import statistics
from datetime import datetime
from pathlib import Path

from quality import (
    DEFAULT_LONG_EDGE,
    QUALITY_VERSION,
    compute_quality_raw,
)
from cli import BASE_DIR, load_cfg, db_path
from database import connect
from tests.fixtures_gen import generate_fixtures

DIMENSIONS = ("sharpness", "exposure", "noise")


def _percentile(values: list[float], p: float) -> float:
    values = sorted(values)
    k = (len(values) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    frac = k - lo
    return values[lo] * (1 - frac) + values[hi] * frac


def render_md(
    lib_rows: list[dict],
    fix_rows: list[dict],
    ordering_ok: list[tuple[str, bool]],
) -> str:
    def table(values: list[float]) -> str:
        return (
            f"| min {min(values):.4g} | p10 {_percentile(values, 0.10):.4g} "
            f"| median {statistics.median(values):.4g} | p90 {_percentile(values, 0.90):.4g} "
            f"| max {max(values):.4g} |"
        )

    lines = [
        f"# M2 阈值标定（{QUALITY_VERSION}）",
        "",
        f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- 计算尺度：长边 {DEFAULT_LONG_EDGE} 灰度图",
        f"- 真实库照片：{len(lib_rows)} 张",
        f"- 合成 fixture：{len(fix_rows)} 张（3 基图 × (1 基图 + 6 变体)）",
        "",
        "## 原始指标分布",
        "",
    ]

    # per-dimension tables with fixture variants grouped
    for d in DIMENSIONS:
        key = f"{d}_raw"
        lines.append(f"### {d}_raw")
        lines.append("")
        lines.append("| 组 | min | p10 | median | p90 | max |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        lines.append(f"| 真实库 | {table([r[key] for r in lib_rows])}")
        for base in ("shapes", "texture", "dark"):
            for variant in ("base", "blur_light", "blur_strong",
                            "overexposed", "underexposed",
                            "noise_light", "noise_strong"):
                subset = [r[key] for r in fix_rows if r.get("_name") == f"{base}_{variant}"]
                if subset:
                    lines.append(f"| {base}_{variant} | {table(subset)}")
        lines.append("")

    lines.append("## 排序验证（fixture）")
    lines.append("")
    for label, ok in ordering_ok:
        lines.append(f"- {'✓' if ok else '✗'} {label}")
    lines.append("")
    lines.append("## 冻结建议")
    lines.append("")
    lines.append("基于以上分布，建议冻结（两档线性钳制，写入 config.yaml quality.thresholds）：")
    lines.append("")
    lines.append("- sharpness: low = 真实库 p10 附近（低于此记 0），high = 真实库 p90 附近（高于此记 1）")
    lines.append("- exposure: low = 明显裁剪档（fixture 变体 p10），high = 真实库 median 附近")
    lines.append("- noise: low = 真实库 median 附近，high = fixture noise_strong p10 附近")
    lines.append("")
    lines.append("（人工确认后写入 config.yaml 并固定 algorithm_version = "
                 f"{QUALITY_VERSION}；quality_score 聚合公式另行实验冻结。）")
    return "\n".join(lines)


def run() -> Path:
    cfg = load_cfg()
    q_cfg = cfg.get("quality") or {}
    long_edge = int(q_cfg.get("long_edge", DEFAULT_LONG_EDGE))
    conn = connect(db_path(cfg))
    photos = conn.execute(
        "SELECT abs_path FROM photos ORDER BY photo_id"
    ).fetchall()
    conn.close()

    print(f"真实库 {len(photos)} 张 + fixture 18 张，计算 raw 指标…")
    lib_rows: list[dict] = []
    for row in photos:
        raw = compute_quality_raw(row["abs_path"], long_edge=long_edge)
        if raw is None:
            continue
        lib_rows.append(raw)
    print(f"  真实库成功 {len(lib_rows)} 张")

    fixtures = generate_fixtures(BASE_DIR / "work" / "m2_fixtures_calib")
    fix_rows: list[dict] = []
    for name, path in fixtures.items():
        raw = compute_quality_raw(path, long_edge=long_edge)
        if raw is None:
            continue
        raw["_name"] = name
        fix_rows.append(raw)
    print(f"  fixture 成功 {len(fix_rows)} 张")

    ordering_ok: list[tuple[str, bool]] = []
    for base in ("shapes", "texture", "dark"):
        def v(variant: str, key: str) -> float:
            return next(
                r[key] for r in fix_rows if r.get("_name") == f"{base}_{variant}"
            )
        ordering_ok.append((
            f"{base}: sharpness base > blur_light > blur_strong",
            v("base", "sharpness_raw") > v("blur_light", "sharpness_raw")
            > v("blur_strong", "sharpness_raw"),
        ))
        ordering_ok.append((
            f"{base}: exposure base < overexposed 且 base < underexposed",
            v("base", "exposure_raw") < v("overexposed", "exposure_raw")
            and v("base", "exposure_raw") < v("underexposed", "exposure_raw"),
        ))
        ordering_ok.append((
            f"{base}: noise base < noise_light < noise_strong",
            v("base", "noise_raw") < v("noise_light", "noise_raw")
            < v("noise_strong", "noise_raw"),
        ))
    failed = [label for label, ok in ordering_ok if not ok]
    if failed:
        print("  排序验证失败：")
        for label in failed:
            print(f"    ✗ {label}")
    else:
        print("  排序验证：18/18 全部通过")

    out_dir = (
        BASE_DIR / "reports" / "calibration"
        / datetime.now().strftime("%Y%m%d_%H%M%S")
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    md = out_dir / "summary.md"
    md.write_text(render_md(lib_rows, fix_rows, ordering_ok), encoding="utf-8")
    print(f"标定报告：{md}")
    return md


if __name__ == "__main__":
    run()
