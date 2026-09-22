"""M3c: semantic analysis — structured VLM observations, cached per photo.

Design notes (IDEA M3c + 24G VRAM constraint):
- The VLM (llama-server + Qwen3.8-27B-VL, mmproj-Qwen3.8-27B-BF16) needs
  ~24G VRAM when the visual projector is attached: calls are strictly
  SERIAL, one image per call. There is no parallelism in this module.
- Every VLM call asks for a strict JSON object (``SEMANTIC_SCHEMA``). The
  response is parsed and validated; on failure we retry once, and if the
  backend still can't produce valid JSON the photo is marked
  ``semantic_unreliable`` instead of poisoning the library.
- Results are cached in ``semantic_analysis`` keyed by
  (photo_id, model, prompt_version, analysis_version): changing the model
  or the prompt version re-analyzes without losing the old history.
- ``semantic_score`` is a 0-100 observation quality, NOT a composite
  photo score: the composite ranking is the M3b policy's job.
- ``raw_response`` stores the verbatim model output for auditability.
"""
from __future__ import annotations

import base64
import io
import json
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

PROMPT_VERSION = "m3c-v2"
ANALYSIS_VERSION = 1

# Flat derived columns (M3c-v1 compat): scene/subjects/person/context/
# semantic_score are filled from the rich profile on write so every
# existing consumer (intent/pipeline/taste) reads v2 rows unchanged.
_FLAT_KEYS = ("scene", "subjects", "person", "defects", "context",
              "semantic_score", "score_components")

# v2 validation: (field: (kind, required, extra)) — nested structures are
# only type-checked at the top level; the LLM is strict-JSON anyway and a
# second retry coerces shape.  The profile dict must contain at least
# scene + subjects + semantic_score to be stored.
SEMANTIC_SCHEMA: dict[str, tuple] = {
    "scene": (str, True, None),
    "subjects": (list, True, None),
    "relationships": (list, False, None),
    "environment": (dict, False, None),
    "composition": (dict, False, None),
    "defects": (list, True, None),
    "semantic_score": (float, True, (0, 100)),
}

_SYSTEM_PROMPT = (
    "你是照片库的视觉分析器。观察这张照片，只输出一个 JSON 对象，"
    "不要输出任何解释、markdown 或代码围栏。"
)

_USER_PROMPT = """请输出如下结构的 JSON（字段含义：
- scene: 场景类别，从 [风景, 人像, 静物, 食物, 建筑, 街景, 微距, 其他] 中选一个
- subjects: 主要主体列表，每个主体是一个对象 {
    name: 中文短语（如 "女性" / "大海" / "白色步道"）,
    attributes: 可见属性数组（年龄/性别/服装/颜色/材质/外观 等，没有则空）,
    position: 画面位置（前景/中景/背景/中央/左侧/右侧…）,
    role: 画面角色（主要人物/次要人物/主要环境/场景结构 等）,
    facing: 朝向（面向镜头/背对镜头/侧身/面向X 等，可 null）,
    action: 动作（站立/坐着/行走/手持X…，可 null）
  }，最多 6 个主体
- relationships: 主体之间的关系列表，每个是一个中文短句，格式 "谁 动词 谁/什么"，
  例如 "女性 面向 大海" / "两人 并排 坐着" / "咖啡杯 放在 桌上"。没有则空列表
- environment: {location_type: 地点类型, weather: 天气, lighting: 光线, time_of_day: 时段}，未知为 null
- composition: {shot_type: 景别（特写/半身/全身/全景）, subject_position: 主体位置, orientation: 横/竖, depth: 景深/前后景描述}，未知为 null
- defects: 可见缺陷列表（过曝/欠曝/模糊/倾斜/遮挡/噪点 等），无则空列表
- context: 一句话总描述（中文）
- semantic_score: 0-100 的"观察质量"分（画面清晰、主体明确、缺陷少的程度）
- score_components: 可选，各分项 {clarity, subject, defects_avoided}
）示例：
{"scene": "风景",
 "subjects": [
   {"name": "女性", "attributes": ["年轻", "白色连衣裙"], "position": "中央", "role": "主要人物", "facing": "背对镜头", "action": "站立"},
   {"name": "大海", "attributes": ["平静"], "position": "背景", "role": "主要环境"},
   {"name": "白色步道", "position": "前景到中央", "role": "场景结构"}],
 "relationships": ["女性 站在 步道中央", "女性 面向 大海"],
 "environment": {"location_type": "海边", "weather": "晴朗", "lighting": "自然光", "time_of_day": "白天"},
 "composition": {"shot_type": "全身", "subject_position": "中央", "orientation": "横向", "depth": "明显前中后景"},
 "defects": [],
 "context": "一名女性独自站在海边步道上面向大海",
 "semantic_score": 92.0,
 "score_components": {"clarity": 90, "subject": 95, "defects_avoided": 92}}"""


class SemanticError(RuntimeError):
    """Backend unreachable / invalid JSON after retry."""


def analyze_schema(data: dict) -> list[str]:
    """Validate a parsed JSON object against SEMANTIC_SCHEMA.

    Returns a list of human-readable problems (empty = valid).
    """
    problems: list[str] = []
    for field, (kind, required, extra) in SEMANTIC_SCHEMA.items():
        value = data.get(field)
        if value is None:
            if required:
                problems.append(f"缺少必填字段 {field}")
            continue
        if kind is str and not isinstance(value, str):
            problems.append(f"{field} 应为字符串")
        elif kind is list and not isinstance(value, list):
            problems.append(f"{field} 应为数组")
        elif kind is dict and not isinstance(value, dict):
            problems.append(f"{field} 应为对象")
        elif kind is float and not isinstance(value, (int, float)):
            problems.append(f"{field} 应为数字")
        elif kind is float and isinstance(value, (int, float)) and extra:
            lo, hi = extra
            if not (lo <= float(value) <= hi):
                problems.append(f"{field} 超出范围 [{lo}, {hi}]")
    return problems


def _post_json(url: str, payload: dict, timeout: float) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        raise SemanticError(f"VLM 后端 HTTP {exc.code}: {body}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SemanticError(f"VLM 后端不可达 {url}: {exc}") from exc


def _extract_json(text: str) -> dict:
    """Pull the first {...} block out of a model reply and parse it."""
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("响应中没有 JSON 对象")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("JSON 顶层不是对象")
    return data


def _image_to_b64(abs_path: str, max_dim: int = 1280) -> str:
    """JPEG-encoded, max-dim-capped image bytes -> base64 data URL payload.

    Large JPEGs are downscaled (24G VRAM budget: one image per call).
    ARW: use the embedded thumbnail via arw.arw_thumb (PIL).
    HEIC: PIL's native HEIC opener (embedded JPEG preview path).
    """
    path = Path(abs_path)
    # ARW/HEIC are handled by their registered PIL openers (arw.py,
    # pillow-heif) — Image.open is the single path for every format.
    # (import arw only registers the opener; importing it is a no-op
    # when rawpy is missing)
    import arw  # noqa: F401
    from PIL import Image

    img = Image.open(str(path))
    img.load()
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    w, h = img.size
    scale = min(1.0, max_dim / max(w, h))
    if scale < 1.0:
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


def vlm_json_call(
    backend: dict, image_b64: str, max_retries: int = 1
) -> dict:
    """One serial multimodal call; strict JSON in, validated out.

    Returns the validated observation dict (keys per SEMANTIC_SCHEMA plus
    ``semantic_unreliable`` = False). Retries once on parse/validate
    failure; if the backend still can't comply, raises SemanticError.
    """
    base_url = (backend.get("base_url") or "").rstrip("/")
    model = backend.get("model")
    if not base_url or not model:
        raise SemanticError("llama 后端未配置 (config.yaml: llama)")
    extra = backend.get("extra") or {}
    max_tokens = int(backend.get("max_tokens", 4096))
    http_timeout = float(backend.get("http_timeout", 180))

    last_problems: list[str] = []
    for attempt in range(max_retries + 1):
        content: list[dict] = [{"type": "text", "text": _USER_PROMPT}]
        if image_b64:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": image_b64},
                }
            )
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
            "max_tokens": max_tokens,
            "temperature": 0,
            **extra,
        }
        resp = _post_json(f"{base_url}/v1/chat/completions", payload, http_timeout)
        choice = (resp.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content") or ""
        try:
            data = _extract_json(text)
            problems = analyze_schema(data)
            if not problems:
                data.setdefault("raw_response", text)
                data["semantic_unreliable"] = False
                return data
            last_problems = problems
        except (ValueError, json.JSONDecodeError) as exc:
            last_problems = [f"JSON 解析失败: {exc}"]
        # retry: ask the model to fix the shape
        payload["messages"].append(
            {
                "role": "user",
                "content": (
                    "上次输出不符合要求（" + "; ".join(last_problems) + "）。"
                    "只重新输出符合要求的 JSON 对象。"
                ),
            }
        )
    raise SemanticError(
        f"VLM 连续 {max_retries + 1} 次未输出合规 JSON: " + "; ".join(last_problems)
    )


def _subjects_to_flat(subjects: list) -> list[str]:
    """v2 rich subject objects -> v1-style flat name list (compat)."""
    out = []
    for s in subjects or []:
        if isinstance(s, dict):
            name = s.get("name")
            if name:
                out.append(str(name))
        elif s is not None:
            out.append(str(s))
    return out


def _person_to_flat(subjects: list) -> str | None:
    """Derive the v1 `person` string from v2 subject objects."""
    person_words = ("女", "男", "人", "儿童", "小孩", "孩子", "宝宝", "老人",
                    "女士", "先生", "女孩", "男孩", "女人", "男人", "情侣", "夫妻")
    people = [
        s for s in subjects or []
        if isinstance(s, dict)
        and any(w in str(s.get("name", "")) for w in person_words)
        or isinstance(s, dict) and "人物" in str(s.get("role", ""))
    ]
    if not people:
        return None
    n = len(people)
    poses = [str(s["action"]) for s in people if s.get("action")]
    if poses:
        return f"{n}人，" + "，".join(poses)
    return f"{n}人"


def _rel_text(data: dict) -> str | None:
    """Flatten relationships for SQL LIKE coarse recall (M3.4)."""
    rels = data.get("relationships") or []
    return "; ".join(str(r) for r in rels) if rels else None


def upsert_semantic(
    conn,
    photo_id: int,
    model: str,
    prompt_version: str,
    analysis_version: int,
    data: dict,
) -> None:
    """Store one observation row (upsert on the 4-tuple key).

    v2: the full profile is stored in `profile`; the flat v1 columns
    (scene/subjects/person/defects/context/semantic_score) are DERIVED
    from it so existing consumers keep reading them unchanged.
    """
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    components = data.get("score_components")
    subjects = data.get("subjects") or []
    flat_subjects = _subjects_to_flat(subjects)
    flat_person = data.get("person")
    if flat_person is None:
        flat_person = _person_to_flat(subjects)
    conn.execute(
        """
        INSERT INTO semantic_analysis (
          photo_id, model, prompt_version, analysis_version,
          scene, subjects, person, defects, context,
          semantic_score, score_components, raw_response, model_version,
          profile, rel_text, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (photo_id, model, prompt_version, analysis_version)
        DO UPDATE SET
          scene=excluded.scene,
          subjects=excluded.subjects,
          person=excluded.person,
          defects=excluded.defects,
          context=excluded.context,
          semantic_score=excluded.semantic_score,
          score_components=excluded.score_components,
          raw_response=excluded.raw_response,
          model_version=excluded.model_version,
          profile=excluded.profile,
          rel_text=excluded.rel_text,
          created_at=excluded.created_at
        """,
        (
            photo_id,
            model,
            prompt_version,
            analysis_version,
            data.get("scene"),
            json.dumps(flat_subjects, ensure_ascii=False),
            flat_person,
            json.dumps(data.get("defects") or [], ensure_ascii=False),
            data.get("context"),
            float(data["semantic_score"]),
            json.dumps(components, ensure_ascii=False) if components else None,
            data.get("raw_response"),
            data.get("model_version"),
            json.dumps(data, ensure_ascii=False),
            _rel_text(data),
            now,
        ),
    )


def get_semantic(
    conn,
    photo_id: int,
    model: str,
    prompt_version: str = PROMPT_VERSION,
    analysis_version: int = ANALYSIS_VERSION,
) -> dict | None:
    row = conn.execute(
        """
        SELECT sa.*, p.rel_path FROM semantic_analysis sa
        JOIN photos p ON p.photo_id = sa.photo_id
        WHERE sa.photo_id = ? AND sa.model = ?
          AND sa.prompt_version = ? AND sa.analysis_version = ?
        """,
        (photo_id, model, prompt_version, analysis_version),
    ).fetchone()
    if row is None:
        return None
    out = dict(row)
    for key in ("subjects", "defects", "score_components", "profile"):
        if out.get(key):
            try:
                out[key] = json.loads(out[key])
            except json.JSONDecodeError:
                pass
    return out


def mark_unreliable(conn, photo_id: int, model: str, error: str) -> None:
    """A failed VLM call must not block the pipeline: record it, move on."""
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO semantic_analysis (
          photo_id, model, prompt_version, analysis_version,
          scene, semantic_score, raw_response, created_at
        ) VALUES (?, ?, ?, ?, NULL, NULL, ?, ?)
        ON CONFLICT (photo_id, model, prompt_version, analysis_version)
        DO UPDATE SET raw_response=excluded.raw_response, created_at=excluded.created_at
        """,
        (
            photo_id,
            model,
            PROMPT_VERSION,
            ANALYSIS_VERSION,
            f"[semantic_unreliable] {error}",
            now,
        ),
    )


def analyze_photo(
    conn, photo_row: dict, backend: dict, refresh: bool = False
) -> tuple[dict | None, str]:
    """Analyze one photo (cache-aware). Returns (observation|None, action).

    action: "cached" | "new" | "unreliable"
    """
    model = backend.get("model") or ""
    cached = get_semantic(conn, photo_row["photo_id"], model)
    if cached is not None and not refresh:
        return cached, "cached"
    try:
        b64 = _image_to_b64(photo_row["abs_path"])
    except Exception as exc:  # unreadable file -> not a VLM failure
        mark_unreliable(conn, photo_row["photo_id"], model, f"读取失败: {exc}")
        conn.commit()
        return None, "unreliable"
    try:
        data = vlm_json_call(backend, b64)
    except SemanticError as exc:
        mark_unreliable(conn, photo_row["photo_id"], model, str(exc))
        conn.commit()
        return None, "unreliable"
    data["model_version"] = model
    upsert_semantic(conn, photo_row["photo_id"], model, PROMPT_VERSION, ANALYSIS_VERSION, data)
    conn.commit()
    return get_semantic(conn, photo_row["photo_id"], model), "new"


def analyze_batch(
    conn,
    backend: dict,
    limit: int | None = None,
    scene: str | None = None,
    min_score: float | None = None,
    photo_ids: list[int] | None = None,
    refresh: bool = False,
    progress=None,
) -> dict:
    """Serial batch analysis (24G VRAM: never parallel).

    Filters select the candidate set BEFORE hitting the VLM, so a
    landscape-only run analyzes only landscape-relevant candidates.
    Returns stats: {total, new, cached, unreliable, failed}.
    """
    query = (
        "SELECT p.photo_id, p.abs_path, p.rel_path FROM photos p "
        "WHERE 1=1"
    )
    params: list = []
    if photo_ids:
        query += f" AND p.photo_id IN ({','.join('?' * len(photo_ids))})"
        params.extend(photo_ids)
    if scene:
        # Filter on cached observations first (a second pass); uncached
        # photos are always analyzed (they can't be filtered yet).
        query += (
            " AND (NOT EXISTS (SELECT 1 FROM semantic_analysis sa "
            "WHERE sa.photo_id = p.photo_id AND sa.model = ? "
            "AND sa.prompt_version = ? AND sa.analysis_version = ? "
            "AND sa.scene = ?) OR ? = 0)"
        )
        model = backend.get("model") or ""
        params.extend([model, PROMPT_VERSION, ANALYSIS_VERSION, scene, 1])
    if min_score is not None:
        query += (
            " AND (NOT EXISTS (SELECT 1 FROM semantic_analysis sa "
            "WHERE sa.photo_id = p.photo_id AND sa.model = ? "
            "AND sa.prompt_version = ? AND sa.analysis_version = ? "
            "AND sa.semantic_score IS NOT NULL AND sa.semantic_score < ?) OR ? = 0)"
        )
        model = backend.get("model") or ""
        params.extend([model, PROMPT_VERSION, ANALYSIS_VERSION, min_score, 1])
    query += " ORDER BY p.photo_id"
    if limit:
        query += f" LIMIT {int(limit)}"

    photos = [dict(r) for r in conn.execute(query, params)]
    stats: dict[str, float] = {
        "total": len(photos), "new": 0, "cached": 0, "unreliable": 0, "failed": 0
    }
    started = time.monotonic()
    for i, photo in enumerate(photos, 1):
        try:
            _, action = analyze_photo(conn, photo, backend, refresh=refresh)
            stats[action] = stats.get(action, 0) + 1
        except Exception as exc:  # per-photo guard: never kill the batch
            stats["failed"] += 1
            if progress:
                progress(i, len(photos), photo["rel_path"], f"错误: {exc}")
            continue
        if progress:
            progress(i, len(photos), photo["rel_path"], action)
    stats["seconds"] = round(time.monotonic() - started, 1)
    return stats
