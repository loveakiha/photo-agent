"""M3.2 Intent (IDEA §M3.2): natural-language entry point → structured intent.

The entry question the agent asks is:
    > "这次想发什么？"

Preset intents (IDEA §M3.2, verbatim):
    - 刚旅行回来
    - 和朋友出去玩
    - 约会
    - 今天心情不错
    - 生日
    - 最近生活不错
    - 想显得最近生活很丰富
    - 随便帮我选

Free-form intent is parsed by keyword rules (zero VLM, zero file access) into
a structured dict:
    {
      intent_type: str        # preset id or "free"
      scene: str | None       # preferred scene (风景/人像/食物/建筑/静物/其他)
      person: bool | None     # require person in frame (True) / forbid (False)
      person_required: bool   # True if person signal is positive, else False
      tone: str | None        # "natural" | "atmosphere" | "refined" | None
      exclude_overdone: bool  # True if user said "不要太刻意" / "不要刻意"
      time_scope: str         # "today" | "recent" | "all"
      year: int | None        # explicit calendar year, e.g. "2025年" / "去年"
    }

This feeds M3.3 pipeline stage "Photo Retrieval" — the first stage after
User Intent, before Quality Filtering / Similarity Reduction / Taste
Profile / Candidate Ranking / Composition.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime

from semantic import semantic_latest_join

INTENT_VERSION = "m32-v2"
RECENT_DAYS = 30

# Presets from IDEA §M3.2 — order matters for matching (longest first).
PRESETS: list[tuple[str, dict]] = [
    # Each preset maps to a partial intent. Longer phrases are matched first.
    ("想显得最近生活很丰富", {
        "intent_type": "want_rich_life",
        "scene": "风景",
        "person": None,
        "person_required": False,
        "tone": "refined",
        "time_scope": "recent",
    }),
    ("刚旅行回来", {
        "intent_type": "just_back_from_trip",
        "scene": "风景",
        "person": None,
        "person_required": False,
        "tone": "natural",
        "time_scope": "recent",
    }),
    ("和朋友出去玩", {
        "intent_type": "out_with_friends",
        "scene": None,
        "person": True,
        "person_required": True,
        "tone": "natural",
        "time_scope": "recent",
    }),
    ("今天心情不错", {
        "intent_type": "feeling_good_today",
        "scene": None,
        "person": None,
        "person_required": False,
        "tone": "natural",
        "time_scope": "today",
    }),
    ("最近生活不错", {
        "intent_type": "life_good_recently",
        "scene": "风景",
        "person": None,
        "person_required": False,
        "tone": "refined",
        "time_scope": "recent",
    }),
    ("随便帮我选", {
        "intent_type": "pick_any",
        "scene": None,
        "person": None,
        "person_required": False,
        "tone": None,
        "time_scope": "all",
    }),
    ("约会", {
        "intent_type": "date",
        "scene": None,
        "person": True,
        "person_required": True,
        "tone": "refined",
        "time_scope": "recent",
    }),
    ("生日", {
        "intent_type": "birthday",
        "scene": None,
        "person": True,
        "person_required": True,
        "tone": "refined",
        "time_scope": "recent",
    }),
]

# Scene keywords (longest first so multi-char beats shorter).
_SCENE_KEYWORDS = [
    ("风景", ["风景", "山", "海", "湖", "公园", "花园", "旅行", "出游",
             "郊外", "徒步", "爬山", "海边", "沙滩", "森林", "草地", "园林",
             "街景", "城市", "夜景", "日落", "日出", "天空", "晚霞", "云海"]),
    ("人像", ["人像", "自拍", "合影", "人物照", "人物", "肖像", "大头照"]),
    ("食物", ["食物", "美食", "吃饭", "餐厅", "菜", "虾", "海鲜", "烤肉",
             "火锅", "寿司", "日料", "蛋糕", "甜品", "咖啡", "奶茶", "早餐",
             "午餐", "晚餐", "宵夜"]),
    ("建筑", ["建筑", "房子", "房间", "公寓", "酒店", "博物馆", "教堂",
             "美术馆", "展览", "商场", "办公室", "工位", "会议室"]),
    ("静物", ["静物", "花", "植物", "书", "书桌上", "桌面", "手办", "摆件",
             "猫", "狗", "宠物", "咖啡杯", "杯子"]),
]

_TONE_KEYWORDS = [
    ("natural", ["自然", "朴素", "真实", "生活感", "不修饰", "朴素"]),
    ("atmosphere", ["氛围", "氛围感", "感觉", "格调", "调调", "意境"]),
    ("refined", ["精致", "质感", "高级", "好看", "好看一点", "精心", "漂亮"]),
]

# Negative signals.
_EXCLUDE_OVERDONE_PATTERNS = [
    "不要太刻意", "不要刻意", "别太刻意", "别刻意", "不要做作",
    "别做作", "不要摆拍", "别摆拍", "不要硬凹",
]

_TIME_KEYWORDS = [
    ("today", ["今天", "今晚", "今早", "刚刚", "刚才", "现在"]),
    ("recent", ["最近", "这几天", "这两天", "这几天", "上周", "这几天"]),
]


def parse_intent(text: str) -> dict:
    """Parse a natural-language intent into a structured dict.

    Rules:
      * Preset phrases are matched first (substring, longest-first).
      * Free-form: scene keywords, person signals, tone, exclude_overdone,
        time scope.
      * Empty / whitespace-only text → intent_type="empty".
    """
    if text is None:
        return _empty_intent()
    s = text.strip()
    if not s:
        return _empty_intent()

    # 1. Preset match (longest first). Free-form signals detected in the
    #    same sentence override the preset's defaults — an explicit "人像"
    #    wins over a preset that defaults to "风景".
    for phrase, preset in _sorted_presets():
        if phrase in s:
            scene = _detect_scene(s) or preset.get("scene")
            person, person_required = _detect_person(s)
            if person is None:
                person, person_required = preset.get("person"), preset.get("person_required", False)
            tone = _detect_tone(s) or preset.get("tone")
            # Only override time_scope if an explicit time keyword is present.
            ts = _detect_time(s)
            time_scope = ts if ts != "all" else preset.get("time_scope", "all")
            return {
                "intent_type": preset["intent_type"],
                "scene": scene,
                "person": person,
                "person_required": person_required,
                "tone": tone,
                "exclude_overdone": _has_exclude_overdone(s),
                "time_scope": time_scope,
                "year": _detect_year(s),
                "_matched_preset": phrase,
            }

    # 2. Free-form.
    scene = _detect_scene(s)
    person, person_required = _detect_person(s)
    tone = _detect_tone(s)
    time_scope = _detect_time(s)
    return {
        "intent_type": "free",
        "scene": scene,
        "person": person,
        "person_required": person_required,
        "tone": tone,
        "exclude_overdone": _has_exclude_overdone(s),
        "time_scope": time_scope,
        "year": _detect_year(s),
    }


def _empty_intent() -> dict:
    return {
        "intent_type": "empty",
        "scene": None,
        "person": None,
        "person_required": False,
        "tone": None,
        "exclude_overdone": False,
        "time_scope": "all",
        "year": None,
    }


def _sorted_presets():
    return sorted(PRESETS, key=lambda p: -len(p[0]))


def _detect_scene(text: str) -> str | None:
    for scene, kws in _SCENE_KEYWORDS:
        for kw in kws:
            if kw in text:
                return scene
    return None


def _detect_person(text: str) -> tuple[bool | None, bool]:
    """Return (person, person_required) without treating the pronoun "我" as a
    person request by itself.

    The old rule matched any occurrence of "我", so ordinary requests such as
    "我想发海边照片" accidentally became person-required. Explicit visual
    signals are required now.
    """
    negative = [
        "不要我", "不露脸", "别露脸", "不要露脸", "没有人",
        "不想出现", "不要出镜", "别出镜", "不要人物", "不要有人",
    ]
    if any(kw in text for kw in negative):
        return False, False

    positive = [
        "我的自拍", "我露脸", "有我", "带我", "我出镜", "我的照片",
        "朋友", "合影", "人物", "女朋友", "男朋友", "约会", "情侣",
        "闺蜜", "家人", "孩子", "宝宝", "自拍",
    ]
    if any(kw in text for kw in positive):
        return True, True
    return None, False


def _detect_tone(text: str) -> str | None:
    for tone, kws in _TONE_KEYWORDS:
        for kw in kws:
            if kw in text:
                return tone
    return None


def _detect_time(text: str) -> str:
    for scope, kws in _TIME_KEYWORDS:
        for kw in kws:
            if kw in text:
                return scope
    return "all"



def _detect_year(text: str) -> int | None:
    """Detect an explicit calendar year or common relative-year phrase."""
    match = re.search(r"(?:19|20)\d{2}年?", text)
    if match:
        return int(match.group(0).rstrip("年"))
    current = datetime.now().year
    if "去年" in text:
        return current - 1
    if "前年" in text:
        return current - 2
    if "今年" in text:
        return current
    return None

def _has_exclude_overdone(text: str) -> bool:
    return any(p in text for p in _EXCLUDE_OVERDONE_PATTERNS)


# ---------------------------------------------------------------------------
# M3.3 pipeline stage: Photo Retrieval
# ---------------------------------------------------------------------------

def _person_is_present(person: str | None) -> bool:
    """semantic.person can be None, empty string, "true"/"false", or a
    JSON list of person names. Treat non-empty as present."""
    if person is None:
        return False
    s = str(person).strip()
    if not s:
        return False
    if s.lower() in ("false", "0", "no"):
        return False
    if s.startswith("["):
        try:
            arr = json.loads(s)
            return len(arr) > 0
        except json.JSONDecodeError:
            return True
    return True


def retrieve_candidates(conn: sqlite3.Connection, intent: dict,
                        limit: int = 50) -> list[dict]:
    """M3.3 stage 1: Photo Retrieval — pull candidates matching the intent.

    Uses semantic_analysis for scene/person/quality signals; falls back to
    photos.person when semantic row is absent. Sorted by semantic_score
    desc (NULL → 0). No Taste Profile / near-dup dedup here — those are
    downstream stages.

    Intent filters:
      * scene  → semantic.scene = ?
      * person → require OR exclude person-present frames
      * year → explicit calendar-year filter (including 去年/前年/今年)
      * time_scope → today/recent filters; recent means the last RECENT_DAYS
        calendar days relative to the local machine date
    """
    scene = intent.get("scene")
    person = intent.get("person")
    year = intent.get("year")
    time_scope = intent.get("time_scope", "all")

    where = ["1=1"]
    params: list = []
    if scene:
        where.append("sa.scene = ?")
        params.append(scene)
    if year is not None:
        where.append("p.taken_at IS NOT NULL AND CAST(substr(p.taken_at, 1, 4) AS INTEGER) = ?")
        params.append(int(year))
    if time_scope == "today":
        where.append("p.taken_at IS NOT NULL AND date(p.taken_at) = date('now', 'localtime')")
    elif time_scope == "recent" and year is None:
        where.append(
            f"p.taken_at IS NOT NULL AND date(p.taken_at) >= "
            f"date('now', 'localtime', '-{RECENT_DAYS} days')"
        )

    if person is True:
        where.append("""(
            sa.person IS NOT NULL
            AND sa.person NOT IN ('', 'false', '0', 'no', 'null')
        )""")
    elif person is False:
        # Negative person intent is a hard retrieval filter in M3.3.
        # If the user wants "landscape but a distant person is acceptable",
        # that is a later soft-ranking/composition concern.
        where.append("""(
            sa.person IS NULL
            OR sa.person IN ('', 'false', '0', 'no', 'null')
        )""")

    sql = (
        f"""
        SELECT p.photo_id, p.rel_path, p.taken_at,
               sa.scene, sa.person AS person,
               sa.semantic_score, sa.subjects
        FROM photos p
        {semantic_latest_join()}
        WHERE {' AND '.join(where)}
        ORDER BY COALESCE(sa.semantic_score, 0) DESC, p.rel_path
        LIMIT ?
        """
    )
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def intent_summary(intent: dict) -> str:
    """One-line human-readable intent for the CLI."""
    if intent.get("intent_type") == "empty":
        return "（空意图 — 没有输入）"
    parts = [f"intent={intent['intent_type']}"]
    if intent.get("year") is not None:
        parts.append(f"year={intent['year']}")
    if intent.get("scene"):
        parts.append(f"scene={intent['scene']}")
    if intent.get("person") is True:
        parts.append("person=要求露脸")
    elif intent.get("person") is False:
        parts.append("person=不露脸")
    if intent.get("tone"):
        parts.append(f"tone={intent['tone']}")
    if intent.get("exclude_overdone"):
        parts.append("不要太刻意")
    if intent.get("time_scope") != "all":
        parts.append(f"time={intent['time_scope']}")
    matched = intent.get("_matched_preset")
    if matched:
        parts.append(f"matched={matched}")
    return " | ".join(parts)
