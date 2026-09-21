"""M3.3 Intent -> Selection -> Composition (IDEA §M3.3).

End-to-end local pipeline, zero VLM:

    User Intent          parse_intent (intent.py)
    ↓
    Photo Retrieval      retrieve_candidates (intent.py)  — coarse recall
    ↓
    Photo Understanding  semantic_analysis (already stored)
    ↓
    Quality Filtering    semantic_score >= quality_floor  (optional)
    ↓
    Similarity Reduction rank_top_n's near-dup dedup
                         (taste.py) — one slot per group, user winner pinned
    ↓
    Taste Profile        build_taste_profile (taste.py)
    ↓
    Candidate Ranking    rank_top_n (taste.py) — semantic + taste_bias
    ↓
    Composition          (M3.4, later — multi-variant)
    ↓
    Preview              (CLI renders contact sheet)

``select_photos`` runs all stages and returns the final ranked candidates.
``pipeline_summary`` renders the per-stage counts so the CLI can show the
funnel.
"""
from __future__ import annotations

import sqlite3

from intent import parse_intent, retrieve_candidates
from taste import build_taste_profile, taste_bias


def _quality_floor_from_cfg(cfg) -> float | None:
    """Read the M2 quality floor from config (semantic score floor)."""
    try:
        return float(cfg.get("semantic_score_floor", 0.0))
    except (AttributeError, TypeError, ValueError):
        return None


def select_photos(
    conn: sqlite3.Connection,
    intent_text: str,
    n: int = 5,
    semantic_score_floor: float | None = None,
) -> dict:
    """Run the full M3.3 pipeline for a natural-language intent.

    Returns:
      {
        intent: dict            # parsed intent
        profile: dict           # taste profile (empty if no user decisions)
        stages: dict            # per-stage counts (the funnel)
        candidates: list[dict]  # final ranked candidates (top-N)
      }
    """
    intent = parse_intent(intent_text)
    profile = build_taste_profile(conn)

    # Stage 1: coarse retrieval (intent filters only, no dedup).
    recalled = retrieve_candidates(conn, intent, limit=200)
    n_recalled = len(recalled)

    # Stage 2: quality floor (semantic_score). We filter the recalled set.
    if semantic_score_floor is not None:
        recalled = [
            r for r in recalled
            if (r["semantic_score"] or 0) >= semantic_score_floor
        ]
    n_after_quality = len(recalled)

    # Stage 3-5: Similarity Reduction + Taste Profile + Candidate Ranking,
    # handled together by rank_top_n (it applies taste_bias and dedups
    # near-dup groups, pinning user-confirmed winners). We re-run the scene
    # query inside rank_top_n — but to respect an arbitrary intent (which may
    # be scene=None / person-based), we instead rank the recalled set here.
    #
    # For a scene-scoped intent, rank_top_n gives the dedup'd top-N directly.
    # For a broader intent (person / no scene), we rank the recalled list by
    # semantic_score + taste_bias with our own dedup, reusing the group data.
    candidates = _rank_recalled(conn, recalled, intent, profile, n)
    n_final = len(candidates)

    return {
        "intent": intent,
        "profile": profile,
        "stages": {
            "recalled": n_recalled,
            "after_quality": n_after_quality,
            "final": n_final,
        },
        "candidates": candidates,
    }


def _rank_recalled(conn, recalled, intent, profile, n) -> list[dict]:
    """Rank a recalled set by semantic_score + taste_bias, dedup near-dup
    groups, pinning user winners. This is the Similarity Reduction + Taste
    Profile + Candidate Ranking stages applied to an arbitrary recalled set."""
    # group_id -> member ids and user winner.
    group_of: dict[int, int] = {}
    for row in conn.execute("SELECT photo_id, group_id FROM group_members"):
        group_of.setdefault(row["photo_id"], row["group_id"])
    user_winner: dict[int, int] = {}
    for row in conn.execute(
        "SELECT photo_id, status FROM decisions WHERE source='user' AND status='KEEP'"
    ):
        gid = group_of.get(row["photo_id"])
        if gid is not None:
            user_winner[gid] = row["photo_id"]

    scored = []
    for r in recalled:
        pid = r["photo_id"]
        # exposure for brightness bias — pull from quality if available.
        q = conn.execute(
            "SELECT exposure FROM quality WHERE photo_id=?", (pid,)
        ).fetchone()
        exposure = q["exposure"] if q else None
        bias = taste_bias(profile, r["scene"], exposure, r["person"])
        scored.append({
            "photo_id": pid,
            "rel_path": r["rel_path"],
            "scene": r["scene"],
            "person": r["person"],
            "semantic_score": r["semantic_score"] or 0.0,
            "bias": bias,
            "final_score": round((r["semantic_score"] or 0.0) + bias, 2),
            "group_id": group_of.get(pid),
        })
    scored.sort(key=lambda x: (-x["final_score"], x["rel_path"]))

    out = []
    seen: set[int] = set()
    for s in scored:
        gid = s["group_id"]
        if gid is not None:
            if gid in seen:
                continue
            pinned = user_winner.get(gid)
            if pinned is not None and s["photo_id"] != pinned:
                continue
            seen.add(gid)
        out.append(s)
        if len(out) == n:
            break
    return out


def pipeline_summary(result: dict) -> str:
    """Render the funnel for the CLI."""
    stages = result["stages"]
    p = result["profile"]
    lines = [
        f"漏斗: 召回 {stages['recalled']} → 质量过滤 {stages['after_quality']} "
        f"→ 最终 {stages['final']}",
        f"画像: {p['n_kept']} KEEP / {p['n_discarded']} DISCARD "
        f"({p['n_samples']} 样本)" + ("（空）" if not p["n_samples"] else ""),
    ]
    return "\n".join(lines)
