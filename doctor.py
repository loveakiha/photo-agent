"""Environment and dependency self-checks."""
from __future__ import annotations

import json
import platform
import sqlite3
import urllib.request


def _import_check(name: str, label: str, required: bool):
    try:
        mod = __import__(name)
        version = getattr(mod, "__version__", "")
        return ("✓", f"{label} {version}".strip())
    except ImportError:
        mark = "✗" if required else "—"
        suffix = "" if required else "（当前阶段可选）"
        return (mark, f"{label} 未安装{suffix}")


def run_checks(cfg: dict):
    results = [
        ("✓", f"Python {platform.python_version()}"),
        ("✓", f"SQLite {sqlite3.sqlite_version}"),
        _import_check("yaml", "PyYAML", True),
        _import_check("PIL", "Pillow", True),
        _import_check("typer", "Typer", True),
        _import_check("pytest", "Pytest", True),
        _import_check("pillow_heif", "pillow-heif（HEIC）", True),
        _import_check("numpy", "numpy（M1）", False),
        _import_check("cv2", "OpenCV（M2）", True),
        _import_check("imagehash", "imagehash（M1）", True),
    ]

    endpoint = (cfg.get("vlm") or {}).get("endpoint", "")
    if not endpoint:
        results.append(("—", "VLM endpoint 未配置（M3 需要，不阻塞 M0–M2）"))
    else:
        try:
            with urllib.request.urlopen(
                endpoint.rstrip("/") + "/models", timeout=5
            ) as response:
                data = json.loads(response.read())
            models = [item.get("id") for item in data.get("data", [])]
            results.append(
                ("✓", f"VLM endpoint 可达 | 模型：{', '.join(models) or '(无)'}")
            )
        except Exception as exc:
            results.append(("✗", f"VLM endpoint 不可达：{exc}"))

    return results
