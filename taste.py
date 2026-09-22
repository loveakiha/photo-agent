"""M3.1 Taste Profile (IDEA §M3.1 + §M3.3 "Taste Profile -> Candidate Ranking").

The long-term object is NOT a "personality" but:

    > how the user likes to be seen by others.

What we can actually learn from the data stored so far:
  * explicit user decisions   (decisions.source='user', status KEEP/DISCARD)
  * pairwise preferences      (preference_samples.source='user', winner)
  * the semantic/quality facts attached to each photo (scene, person,
    exposure) — those turn the decisions into measurable tastes.

This module is ZERO-VLM and ZERO-file-access: it reads only stored facts and
returns an explainable profile plus a bounded additive bias that a later
ranking stage can apply. Every number in the profile carries a count, and
the bias clamps to a documented range so it can never override a clearly
worse photo.

Pipeline position (IDEA §M3.3):

    ... Quality Filtering -> Similarity Reduction -> **Taste Profile**
        -> Candidate Ranking -> Composition
"""
from __future__ import annotations

import json
import sqlite3

PROFILE_VERSION = "m31-v1"

# Scene labels as the VLM emits them (see semantic.analyze_prompt). We map
# the IDEA "landscape / friends / food / travel" axes onto the observed scene
# vocabulary; unknown scenes are folded into "其他".
KNOWN_SCENES = ("风景", "人像", "食物", "建筑", "静物", "其他")

# The additive bias is clamped to this range. A taste signal can nudge the
# ranking by at most BIAS_MAX points — never enough to leapfrog a 20-point
# quality gap, which keeps the profile a tie-breaker / re-arranger, not an
# override (IDEA §M3.3 taste is one input to Candidate Ranking, not the
# whole ranking).
BIAS_MAX = 6.0


def _photo_facts(conn: sqlite3.Connection) -> dict:
    """One row per analyzed photo: scene, person, exposure, semantic_score."""
    rows = conn.execute(
        """
        SELECT sa.photo_id, sa.scene, sa.person, sa.semantic_score,
               q.exposure, p.rel_path
        FROM semantic_analysis sa
        JOIN photos p ON p.photo_id = sa.photo_id
        LEFT JOIN quality q ON q.photo_id = sa.photo_id
        WHERE sa.semantic_score IS NOT NULL
        """
    ).fetchall()
    facts = {}
    for r in rows:
        facts[r["photo_id"]] = {
            "scene": r["scene"] or "其他",
            "person": (r["person"] or "").strip().lower(),
            "exposure": r["exposure"],
            "semantic_score": r["semantic_score"],
            "rel_path": r["rel_path"],
        }
    return facts


def _scene_of(scene: str | None) -> str:
    return scene if scene in KNOWN_SCENES else "其他"


def _mean(values) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def build_taste_profile(conn: sqlite3.Connection, user_id: str = "local") -> dict:
    """Aggregate the user's explicit choices into an explainable profile.

    Signals (both optional — an empty profile is returned when there are no
    user decisions yet):
      * decisions.source='user'  status KEEP / DISCARD
      * preference_samples.source='user' with a non-NULL winner

    Returns:
      {
        version, n_kept, n_discarded, n_samples,
        scene_affinity: {scene: lift}   # kept-share / baseline-share - 1
        person_share: float | None      # fraction of kept photos with a face
        exposure_mean: float | None     # mean M2 exposure of kept photos
        exposure_lift: float | None     # kept exposure - all-photos exposure
        high_score_share: float | None  # fraction of kept at >= 90
      }
    """
    facts = _photo_facts(conn)

    kept_ids: set[int] = set()
    discarded_ids: set[int] = set()
    for row in conn.execute(
        "SELECT photo_id, status FROM decisions "
        "WHERE source = 'user' AND status IN ('KEEP', 'DISCARD')"
    ):
        (kept_ids if row["status"] == "KEEP" else discarded_ids).add(row["photo_id"])

    # Pairwise preferences: the winner is a KEEP signal, the loser a DISCARD.
    for row in conn.execute(
        """SELECT candidate_a, candidate_b, winner FROM preference_samples
           WHERE source = 'user' AND winner IS NOT NULL"""
    ):
        a, b, w = row["candidate_a"], row["candidate_b"], row["winner"]
        kept_ids.add(w)
        loser = a if w == b else b
        if loser is not None and loser != w:
            discarded_ids.add(loser)

    kept_ids &= set(facts)
    discarded_ids &= set(facts)

    profile = {
        "version": PROFILE_VERSION,
        "n_kept": len(kept_ids),
        "n_discarded": len(discarded_ids),
        "n_samples": len(kept_ids) + len(discarded_ids),
        "scene_affinity": {},
        "person_share": None,
        "exposure_mean": None,
        "exposure_lift": None,
        "high_score_share": None,
    }
    if not profile["n_samples"]:
        return profile
    # DISCARD-only history contains negative evidence but no positive taste
    # anchor. It cannot produce a meaningful kept-share distribution, and
    # dividing by n_kept would otherwise crash. Keep the sample counts for
    # auditability but leave taste features neutral until at least one KEEP
    # signal exists.
    if not profile["n_kept"]:
        return profile

    # Baseline scene distribution across ALL analyzed photos.
    baseline_counts = {s: 0 for s in KNOWN_SCENES}
    for f in facts.values():
        baseline_counts[_scene_of(f["scene"])] += 1
    baseline_total = max(1, len(facts))

    # Scene affinity: how much the user over/under-indexes each scene.
    kept_counts = {s: 0 for s in KNOWN_SCENES}
    kept_person = 0
    kept_exposures = []
    kept_high = 0
    for pid in kept_ids:
        f = facts[pid]
        kept_counts[_scene_of(f["scene"])] += 1
        if f["person"] and f["person"] not in ("false", "0", "no", ""):
            kept_person += 1
        if f["exposure"] is not None:
            kept_exposures.append(f["exposure"])
        if (f["semantic_score"] or 0) >= 90:
            kept_high += 1

    for scene in KNOWN_SCENES:
        base_share = baseline_counts[scene] / baseline_total
        kept_share = kept_counts[scene] / profile["n_kept"]
        # lift = kept_share / base_share - 1  (0 means "no preference")
        profile["scene_affinity"][scene] = (
            round(kept_share / base_share - 1.0, 3) if base_share > 0 else 0.0
        )

    profile["person_share"] = kept_person / profile["n_kept"]
    profile["exposure_mean"] = _mean(kept_exposures)
    all_exposure = _mean([f["exposure"] for f in facts.values()])
    profile["exposure_lift"] = (
        round(profile["exposure_mean"] - all_exposure, 3)
        if profile["exposure_mean"] is not None and all_exposure is not None
        else None
    )
    profile["high_score_share"] = kept_high / profile["n_kept"]
    return profile


def taste_bias(profile: dict, scene: str | None,
               exposure: float | None, person: str | None) -> float:
    """Bounded additive bias for one candidate given the user's profile.

    Combines scene affinity (dominant signal) and brightness alignment.
    Clamped to [-BIAS_MAX, +BIAS_MAX]. A profile with no samples returns 0.
    """
    if not profile.get("n_samples"):
        return 0.0
    bias = 0.0

    lift = profile.get("scene_affinity", {}).get(_scene_of(scene), 0.0)
    # Affinity lift is e.g. +0.5 (50% over-index) / -0.3 (under-index).
    # Scale so a 1.0 lift (user only keeps this scene) saturates the bias.
    bias += max(-1.0, min(1.0, lift)) * BIAS_MAX

    # Brightness: if the user's kept photos run brighter (positive lift),
    # reward brighter candidates proportionally (exposure is 0-1).
    exp_lift = profile.get("exposure_lift")
    if exp_lift is not None and exposure is not None:
        # exposure ~0.5 is neutral; deviation from 0.5 is in [-0.5, 0.5].
        deviation = exposure - 0.5
        bias += max(-1.0, min(1.0, exp_lift * 4.0)) * (deviation * 2.0) * (BIAS_MAX / 2)

    return round(max(-BIAS_MAX, min(BIAS_MAX, bias)), 3)


def rank_top_n(
    conn: sqlite3.Connection,
    scene: str,
    n: int = 5,
    profile: dict | None = None,
) -> list[dict]:
    """Taste-aware top-N for a scene.

    Score = semantic_score + taste_bias, with near-duplicate dedup so one
    near-dup group occupies at most one slot (a group the user has already
    decided is pinned to its user-confirmed winner). Pure local — no VLM.
    """
    profile = profile or build_taste_profile(conn)
    facts = _photo_facts(conn)

    # group_id -> member photo_ids (for dedup + pinning user winners).
    group_of: dict[int, int] = {}
    for row in conn.execute(
        "SELECT m.photo_id, m.group_id FROM group_members m"
    ):
        group_of.setdefault(row["photo_id"], row["group_id"])

    user_winner_by_group: dict[int, int] = {}
    for row in conn.execute(
        """SELECT d.photo_id, d.status FROM decisions d
           WHERE d.source='user' AND d.status='KEEP'"""
    ):
        gid = group_of.get(row["photo_id"])
        if gid is not None:
            user_winner_by_group[gid] = row["photo_id"]

    rows = conn.execute(
        """
        SELECT sa.photo_id, sa.scene, sa.person, sa.semantic_score,
               q.exposure, p.rel_path
        FROM semantic_analysis sa
        JOIN photos p ON p.photo_id = sa.photo_id
        LEFT JOIN quality q ON q.photo_id = sa.photo_id
        WHERE sa.scene = ? AND sa.semantic_score IS NOT NULL
        """,
        (scene,),
    ).fetchall()

    scored = []
    for r in rows:
        f = facts[r["photo_id"]]
        bias = taste_bias(profile, f["scene"], f["exposure"], f["person"])
        scored.append({
            "photo_id": r["photo_id"],
            "rel_path": r["rel_path"],
            "scene": r["scene"],
            "semantic_score": r["semantic_score"],
            "bias": bias,
            "final_score": round(r["semantic_score"] + bias, 2),
            "group_id": group_of.get(r["photo_id"]),
        })
    scored.sort(key=lambda x: (-x["final_score"], x["rel_path"]))

    # One slot per near-dup group; if the user pinned a winner for a group,
    # that member is the only one that can represent the group.
    out: list[dict] = []
    seen_groups: set[int] = set()
    for s in scored:
        gid = s["group_id"]
        if gid is not None:
            if gid in seen_groups:
                continue
            pinned = user_winner_by_group.get(gid)
            if pinned is not None and s["photo_id"] != pinned:
                # The user already chose a different member for this group;
                # skip this one, the pinned member will (re)appear below if
                # it scored high enough to reach a slot.
                continue
            seen_groups.add(gid)
        out.append(s)
        if len(out) == n:
            break
    return out


def profile_summary(profile: dict) -> str:
    """Human-readable one-screen profile for the CLI."""
    if not profile.get("n_samples"):
        return "暂无用户决定 — 画像为空（先做几组 confirm / preference 后再看）。"
    lines = [
        f"口味画像 ({profile['version']})：KEEP {profile['n_kept']} / "
        f"DISCARD {profile['n_discarded']}（{profile['n_samples']} 样本）",
        "  场景偏好 (lift, >0 表示偏爱):",
    ]
    aff = profile.get("scene_affinity", {})
    for scene, lift in sorted(aff.items(), key=lambda kv: -kv[1]):
        if abs(lift) < 0.05:
            continue
        arrow = "↑" if lift > 0 else "↓"
        lines.append(f"    {scene} {arrow} {lift:+.2f}")
    if profile.get("person_share") is not None:
        lines.append(f"  露脸比例 (kept): {profile['person_share']:.2f}")
    if profile.get("exposure_lift") is not None:
        lines.append(f"  明暗偏好 (kept - all): {profile['exposure_lift']:+.3f}")
    if profile.get("high_score_share") is not None:
        lines.append(f"  高分(>=90) 占比 (kept): {profile['high_score_share']:.2f}")
    return "\n".join(lines)
