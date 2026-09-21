"""M3.0: read-only Tool API — the photo knowledge base exposed to the LLM.

Architecture boundary (fixed in the M3 spec):
- Python tools answer FACTS from the database. They never guess at intent,
  never compose natural language, and never touch the filesystem in any
  way that modifies user files.
- The LLM does the understanding and planning; it picks which tool to call
  next. The only mutation these tools can cause is writing a *report* file
  into the project's own reports/ directory (generate_report), which is the
  same operation the existing ``report`` command performs.

Tool registry: :data:`TOOLS` maps tool name -> (OpenAI function schema,
handler). Handlers take ``(conn, **kwargs)`` and return JSON-serializable
dicts. Results are truncated by ``max_results`` so a single tool call can
never flood the model's context (M3 guardrail: max return size).
"""
from __future__ import annotations

from database import connect  # noqa: F401  (re-exported for CLI wiring)
from typing import Callable
from pathlib import Path

from similarity import hamming_distance

DEFAULT_MAX_RESULTS = 50
# Hard cap: even an explicit larger request cannot return more than this.
MAX_RESULTS_HARD_CAP = 200

# ---------------------------------------------------------------------------
# schemas
# ---------------------------------------------------------------------------

_SEARCH_PROPS = {
    "year": {
        "type": "integer",
        "description": "拍摄年份（来自 EXIF taken_at），如 2025",
    },
    "date_from": {
        "type": "string",
        "description": "起始日期（含），ISO 格式 YYYY-MM-DD",
    },
    "date_to": {
        "type": "string",
        "description": "截止日期（含），ISO 格式 YYYY-MM-DD",
    },
    "format": {
        "type": "string",
        "description": "文件格式，如 JPEG / HEIF / PNG（不区分大小写）",
    },
    "path_contains": {
        "type": "string",
        "description": "路径子串（区分大小写），如某个文件夹名",
    },
    "min_sharpness": {
        "type": "number",
        "description": "清晰度下限（0~1，越高越清晰）",
    },
    "min_exposure": {
        "type": "number",
        "description": "曝光质量下限（0~1，越高越好）",
    },
    "min_noise": {
        "type": "number",
        "description": "噪点质量下限（0~1，越高噪点越少）",
    },
    "in_group": {
        "type": "string",
        "description": "只取属于该类型分组的照片：exact（精确重复）或 near（近似重复候选）",
    },
    "include_quality": {
        "type": "boolean",
        "description": "结果是否内联三维度质量分（默认 false，省 token）",
    },
    "include_groups": {
        "type": "boolean",
        "description": "结果是否标注每张照片所属的分组 ID（默认 false）",
    },
}

SCHEMAS: dict[str, dict] = {
    "search_photos": {
        "type": "function",
        "function": {
            "name": "search_photos",
            "description": (
                "按条件搜索照片库：时间（年/日期区间）、格式、路径子串、"
                "质量维度下限、所属分组类型。返回 rel_path 列表（可内联质量分"
                "与分组标注）。这是最常用的工具：先 search_photos，再用"
                " get_photo/get_quality 看单张细节。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    **_SEARCH_PROPS,
                    "max_results": {
                        "type": "integer",
                        "description": f"最多返回多少张（默认 {DEFAULT_MAX_RESULTS}，上限 {MAX_RESULTS_HARD_CAP}）",
                    },
                },
            },
        },
    },
    "get_photo": {
        "type": "function",
        "function": {
            "name": "get_photo",
            "description": (
                "查看单张照片的完整信息：元数据（时间/地点/相机/尺寸）、"
                "质量分、所属分组。rel_path 必须是 search_photos 返回的精确值。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "rel_path": {"type": "string", "description": "照片相对路径（精确）"},
                },
                "required": ["rel_path"],
            },
        },
    },
    "get_metadata": {
        "type": "function",
        "function": {
            "name": "get_metadata",
            "description": (
                "查看单张照片的拍摄元数据（taken_at、GPS、相机、尺寸、文件大小、"
                "格式），比 get_photo 更省 token。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "rel_path": {"type": "string", "description": "照片相对路径（精确）"},
                },
                "required": ["rel_path"],
            },
        },
    },
    "get_quality": {
        "type": "function",
        "function": {
            "name": "get_quality",
            "description": (
                "查看单张照片的 M2 技术质量分：sharpness（清晰度）、exposure"
                "（曝光）、noise（噪点），0=明显问题端，1=良好端，附原始测量值。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "rel_path": {"type": "string", "description": "照片相对路径（精确）"},
                },
                "required": ["rel_path"],
            },
        },
    },
    "get_duplicate_groups": {
        "type": "function",
        "function": {
            "name": "get_duplicate_groups",
            "description": (
                "列出重复分组。kind='exact'：字节级精确重复；kind='near'：近似"
                "重复候选（连通分量语义）。每组给出代表照片（建议保留）和成员的"
                "质量分，方便比较该保留哪张。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["exact", "near", "all"],
                        "description": "要列出的分组类型（默认 all）",
                    },
                    "min_size": {
                        "type": "integer",
                        "description": "只列成员数 >= 该值的组（默认 2）",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": f"最多返回多少组（默认 {DEFAULT_MAX_RESULTS}）",
                    },
                },
            },
        },
    },
    "get_similar_photos": {
        "type": "function",
        "function": {
            "name": "get_similar_photos",
            "description": (
                "查看某张照片所属的近似重复组内其他成员（含与代表的汉明距离），"
                "用于判断它和哪些照片构图/内容接近。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "rel_path": {"type": "string", "description": "照片相对路径（精确）"},
                },
                "required": ["rel_path"],
            },
        },
    },
    "generate_report": {
        "type": "function",
        "function": {
            "name": "generate_report",
            "description": (
                "生成一份完整的 Markdown 扫描/去重报告（写入项目 reports/ 目录，"
                "不改动任何照片文件），返回报告文件路径。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
}

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _clamp_max_results(value) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = DEFAULT_MAX_RESULTS
    return max(1, min(n, MAX_RESULTS_HARD_CAP))


def _photo_row(conn, rel_path: str):
    row = conn.execute(
        "SELECT * FROM photos WHERE rel_path = ?", (rel_path,)
    ).fetchone()
    return row


def _quality_for(conn, photo_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM quality WHERE photo_id = ?", (photo_id,)
    ).fetchone()
    if row is None:
        return None
    return {
        "sharpness": row["sharpness"],
        "exposure": row["exposure"],
        "noise": row["noise"],
        "raw": {
            "sharpness_raw": row["sharpness_raw"],
            "exposure_raw": row["exposure_raw"],
            "noise_raw": row["noise_raw"],
        },
        "algorithm_version": row["algorithm_version"],
    }


def _groups_for_photo(conn, photo_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT g.group_id, g.kind, g.size, g.rep_photo_id, gm.sim_to_rep
        FROM group_members gm JOIN groups g ON g.group_id = gm.group_id
        WHERE gm.photo_id = ?
        """,
        (photo_id,),
    ).fetchall()
    return [
        {
            "group_id": r["group_id"],
            "kind": r["kind"],
            "size": r["size"],
            "is_representative": r["photo_id"] == r["rep_photo_id"],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------


def search_photos(conn, **kwargs) -> dict:
    where: list[str] = ["p.photo_id = (SELECT MAX(p2.photo_id) FROM photos p2 WHERE p2.abs_path = p.abs_path)"]
    params: list = []
    if kwargs.get("year") is not None:
        where.append("CAST(substr(p.taken_at, 1, 4) AS INTEGER) = ?")
        params.append(int(kwargs["year"]))
    if kwargs.get("date_from"):
        where.append("p.taken_at IS NOT NULL AND date(p.taken_at) >= date(?)")
        params.append(str(kwargs["date_from"]))
    if kwargs.get("date_to"):
        where.append("p.taken_at IS NOT NULL AND date(p.taken_at) <= date(?)")
        params.append(str(kwargs["date_to"]))
    if kwargs.get("format"):
        where.append("lower(p.format) = lower(?)")
        params.append(str(kwargs["format"]))
    if kwargs.get("path_contains"):
        where.append("p.rel_path LIKE ?")
        params.append(f"%{kwargs['path_contains']}%")
    for dim in ("min_sharpness", "min_exposure", "min_noise"):
        if kwargs.get(dim) is not None:
            col = dim.removeprefix("min_")
            where.append(f"q.{col} IS NOT NULL AND q.{col} >= ?")
            params.append(float(kwargs[dim]))
    if kwargs.get("in_group"):
        kind = str(kwargs["in_group"])
        if kind not in ("exact", "near"):
            return {"error": f"in_group 只支持 exact/near，收到 {kind!r}"}
        where.append(
            "p.photo_id IN (SELECT gm.photo_id FROM group_members gm "
            "JOIN groups g ON g.group_id = gm.group_id WHERE g.kind = ?)"
        )
        params.append(kind)

    need_quality = any(
        kwargs.get(k) is not None for k in ("min_sharpness", "min_exposure", "min_noise")
    ) or bool(kwargs.get("include_quality"))
    join_quality = "JOIN quality q ON q.photo_id = p.photo_id" if need_quality else ""

    sql = (
        "SELECT p.rel_path, p.photo_id, p.taken_at, p.format, p.width, p.height "
        f"{', q.sharpness, q.exposure, q.noise' if need_quality else ''} "
        f"FROM photos p {join_quality} "
        f"WHERE {' AND '.join(where)} ORDER BY p.rel_path LIMIT ?"
    )
    limit = _clamp_max_results(kwargs.get("max_results"))
    rows = conn.execute(sql, (*params, limit)).fetchall()

    total = conn.execute(
        f"SELECT COUNT(*) AS c FROM photos p {join_quality} WHERE {' AND '.join(where)}",
        params,
    ).fetchone()["c"]

    items = []
    for r in rows:
        item = {
            "rel_path": r["rel_path"],
            "photo_id": r["photo_id"],
            "taken_at": r["taken_at"],
            "format": r["format"],
        }
        if need_quality:
            item["quality"] = {
                "sharpness": r["sharpness"],
                "exposure": r["exposure"],
                "noise": r["noise"],
            }
        if kwargs.get("include_groups"):
            item["groups"] = _groups_for_photo(conn, r["photo_id"])
        items.append(item)
    return {
        "count": len(items),
        "total_matched": total,
        "truncated": total > len(items),
        "items": items,
    }


def get_photo(conn, rel_path: str, **kwargs) -> dict:
    row = _photo_row(conn, rel_path)
    if row is None:
        return {"error": f"照片不存在：{rel_path}（rel_path 必须是精确值）"}
    out = {
        "rel_path": row["rel_path"],
        "photo_id": row["photo_id"],
        "metadata": _metadata_dict(row),
        "quality": _quality_for(conn, row["photo_id"]),
        "groups": _groups_for_photo(conn, row["photo_id"]),
    }
    return out


def get_metadata(conn, rel_path: str, **kwargs) -> dict:
    row = _photo_row(conn, rel_path)
    if row is None:
        return {"error": f"照片不存在：{rel_path}"}
    return {"rel_path": row["rel_path"], "metadata": _metadata_dict(row)}


def _metadata_dict(row) -> dict:
    return {
        "taken_at": row["taken_at"],
        "gps": (
            {"lat": row["gps_lat"], "lng": row["gps_lng"]}
            if row["gps_lat"] is not None and row["gps_lng"] is not None
            else None
        ),
        "camera_make": row["camera_make"],
        "camera_model": row["camera_model"],
        "width": row["width"],
        "height": row["height"],
        "format": row["format"],
        "size_bytes": row["size_bytes"],
        "first_seen": row["first_seen"],
    }


def get_quality(conn, rel_path: str, **kwargs) -> dict:
    row = _photo_row(conn, rel_path)
    if row is None:
        return {"error": f"照片不存在：{rel_path}"}
    quality = _quality_for(conn, row["photo_id"])
    if quality is None:
        return {"error": f"尚未计算质量分（运行 quality 命令）：{rel_path}"}
    return {"rel_path": rel_path, "quality": quality}


def get_duplicate_groups(conn, **kwargs) -> dict:
    kind = kwargs.get("kind", "all")
    if kind not in ("exact", "near", "all"):
        return {"error": f"kind 只支持 exact/near/all，收到 {kind!r}"}
    try:
        min_size = max(2, int(kwargs.get("min_size", 2)))
    except (TypeError, ValueError):
        min_size = 2
    limit = _clamp_max_results(kwargs.get("max_results"))

    where = "g.size >= ?"
    params: list = [min_size]
    if kind != "all":
        where += " AND g.kind = ?"
        params.append(kind)

    groups = []
    for g in conn.execute(f"SELECT * FROM groups AS g WHERE {where} ORDER BY g.group_id LIMIT ?", (*params, limit)):
        rep = conn.execute("SELECT rel_path FROM photos WHERE photo_id=?", (g["rep_photo_id"],)).fetchone()
        rep_hashes = conn.execute(
            "SELECT phash, dhash FROM photo_hashes WHERE photo_id=?", (g["rep_photo_id"],)
        ).fetchone()
        members = []
        for m in conn.execute(
            """
            SELECT p.rel_path, q.sharpness, q.exposure, q.noise, ph.phash, ph.dhash
            FROM group_members gm
            JOIN photos p ON p.photo_id = gm.photo_id
            LEFT JOIN quality q ON q.photo_id = p.photo_id
            LEFT JOIN photo_hashes ph ON ph.photo_id = p.photo_id
            WHERE gm.group_id = ?
            ORDER BY p.rel_path
            """,
            (g["group_id"],),
        ):
            member = {
                "rel_path": m["rel_path"],
                "is_representative": m["rel_path"] == (rep["rel_path"] if rep else None),
                "quality": {
                    "sharpness": m["sharpness"],
                    "exposure": m["exposure"],
                    "noise": m["noise"],
                },
            }
            if g["kind"] == "near" and rep_hashes is not None and m["phash"] is not None:
                member["hash_delta_to_rep"] = {
                    "phash": hamming_distance(rep_hashes["phash"], m["phash"]),
                    "dhash": hamming_distance(rep_hashes["dhash"], m["dhash"]),
                }
            members.append(member)
        groups.append(
            {
                "group_id": g["group_id"],
                "kind": g["kind"],
                "size": g["size"],
                "representative": rep["rel_path"] if rep else None,
                "members": members,
            }
        )
    return {"count": len(groups), "groups": groups}


def get_similar_photos(conn, rel_path: str, **kwargs) -> dict:
    row = _photo_row(conn, rel_path)
    if row is None:
        return {"error": f"照片不存在：{rel_path}"}
    in_group = conn.execute(
        """
        SELECT 1 FROM group_members gm
        JOIN groups g ON g.group_id = gm.group_id
        WHERE gm.photo_id = ? AND g.kind = 'near'
        """,
        (row["photo_id"],),
    ).fetchone()
    similar = []
    for m in conn.execute(
        """
        SELECT p.rel_path, g.group_id, g.kind, gm.sim_to_rep
        FROM group_members gm
        JOIN groups g ON g.group_id = gm.group_id
        JOIN photos p ON p.photo_id = gm.photo_id
        WHERE g.kind = 'near'
          AND g.group_id IN (
              SELECT gm2.group_id
              FROM group_members gm2
              WHERE gm2.photo_id = ?
          )
        ORDER BY p.rel_path
        """,
        (row["photo_id"],),
    ):
        if m["rel_path"] == rel_path:
            continue
        similar.append(
            {
                "rel_path": m["rel_path"],
                "group_id": m["group_id"],
                "similarity_to_rep": m["sim_to_rep"],
            }
        )
    return {"rel_path": rel_path, "in_near_group": in_group is not None, "similar": similar}


def generate_report(conn, **kwargs) -> dict:
    reports_dir = kwargs.get("reports_dir")
    if reports_dir is None:
        return {"error": "generate_report 缺少 reports_dir 配置"}
    from report import write_report

    path = write_report(conn, Path(reports_dir))
    return {"report_path": str(path)}


HANDLERS: dict[str, Callable] = {
    "search_photos": search_photos,
    "get_photo": get_photo,
    "get_metadata": get_metadata,
    "get_quality": get_quality,
    "get_duplicate_groups": get_duplicate_groups,
    "get_similar_photos": get_similar_photos,
    "generate_report": generate_report,
}


def tool_definitions() -> list[dict]:
    """OpenAI-style tool list for the chat/completions request."""
    return [SCHEMAS[name] for name in HANDLERS]


def call_tool(conn, name: str, args: dict, extra: dict | None = None) -> dict:
    """Dispatch one tool call. Returns a JSON-serializable dict (never raises)."""
    handler = HANDLERS.get(name)
    if handler is None:
        return {"error": f"未知工具：{name}"}
    try:
        return handler(conn, **(args or {}), **((extra or {}) if handler is generate_report else {}))
    except Exception as exc:  # tool errors are data, not crashes
        return {"error": f"{type(exc).__name__}: {exc}"}
