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

    llama = cfg.get("llama") or {}
    base_url = (llama.get("base_url") or "").rstrip("/")
    if not base_url:
        results.append(("—", "llama 后端未配置（M3 需要，不阻塞 M0–M2）"))
    else:
        try:
            with urllib.request.urlopen(
                base_url + "/v1/models", timeout=5
            ) as response:
                data = json.loads(response.read())
            models = [item.get("name") or item.get("id") for item in data.get("models", [])]
            model_cfg = llama.get("model") or ""
            loaded = model_cfg in models if model_cfg else bool(models)
            if loaded:
                results.append(("✓", f"llama 后端可达 | 模型已加载：{model_cfg}"))
            elif models:
                results.append(("⚠", f"llama 后端可达但配置模型未加载 | 已加载：{', '.join(models) or '(无)'}"))
            else:
                results.append(("⚠", "llama 后端可达但未加载任何模型"))
        except Exception as exc:
            results.append(("✗", f"llama 后端不可达：{exc}"))

    return results
