"""M3b Decision Policy: deterministic, explainable recommendations.

Pipeline position (IDEA §17):

    facts (photos / hashes / quality / groups)
        -> Decision Policy (this module, zero AI, zero file access)
        -> recommendations  (decisions rows, source='policy')
        -> user review / confirm (source='user')
        -> M4 execution (not this module)

Design rules (from the project spec):
- Inputs are ONLY stored facts: photos, quality, group membership.
  Nothing here reads files or calls a model — readability is expressed
  as "has dimensions" (the scanner only stores decodable files).
- No hidden "RAW is always better": format preference is expressed as
  file size (RAW > processed JPG of the same scene) and documented in
  the reason string, not as an absolute rule.
- ``quality_score`` is not frozen yet (NULL in the quality table), so the
  quality signal used here is the *worst normalized dimension*
  (min of sharpness/exposure/noise) — the same temporary signal the
  report uses, labeled as such in every reason.
- Every recommendation carries structured evidence (rule_version,
  factors) that later stages (M3.1 preference, M3c semantic, M4
  execution) can consume.
- Policy NEVER touches photos/groups/files; it only writes/overwrites
  decisions rows with source='policy'. User-confirmed rows (source='user')
  are never overwritten.
"""
from __future__ import annotations

import json
import sqlite3

from database import set_decision

RULE_VERSION = "m3b-v1"

# Decision statuses.
#   POLICY_KEEP / POLICY_DISCARD  -> recommendation rows (source='policy')
#   KEEP / DISCARD                -> user-confirmed rows (source='user')
POLICY_KEEP = "POLICY_KEEP"
POLICY_DISCARD = "POLICY_DISCARD"
USER_KEEP = "KEEP"
USER_DISCARD = "DISCARD"

# Metadata completeness: these photos fields count as "rich metadata".
_METADATA_FIELDS = ("taken_at", "gps_lat", "gps_lng", "camera_make", "camera_model")


def metadata_completeness(photo: dict) -> float:
    """Fraction of the tracked EXIF fields that are non-NULL (0.0-1.0)."""
    filled = sum(1 for field in _METADATA_FIELDS if photo.get(field) is not None)
    return filled / len(_METADATA_FIELDS)


def readable_photo(photo: dict) -> bool:
    """A stored photo whose dimensions were measured is decodable/readable.

    The scanner only inserts files it could open, so in practice every
    stored row is readable; this guard keeps the rule explicit and safe
    against manual/partial rows.
    """
    return photo.get("width") is not None and photo.get("height") is not None


def resolution(photo: dict) -> int:
    """Megapixels (0 when dimensions unknown)."""
    return int((photo.get("width") or 0) * (photo.get("height") or 0))


def worst_dimension(photo: dict, quality: dict | None) -> float | None:
    """Worst normalized M2 dimension (min of non-None), or None."""
    if not quality:
        return None
    dims = [quality.get(k) for k in ("sharpness", "exposure", "noise")]
    dims = [d for d in dims if d is not None]
    return min(dims) if dims else None


class _Candidate:
    """One member of a group with its extracted facts."""

    def __init__(self, photo: dict, quality: dict | None):
        self.photo = photo
        self.quality = quality
        self.readable = readable_photo(photo)
        self.meta = metadata_completeness(photo)
        self.resolution = resolution(photo)
        self.size = photo.get("size_bytes") or 0
        self.worst = worst_dimension(photo, quality)
        self.quality_ok = self.worst is not None

    @property
    def key(self) -> int:
        return int(self.photo["photo_id"])

    @property
    def path(self) -> str:
        return str(self.photo.get("rel_path") or self.photo.get("abs_path"))


def _evidence(candidate: _Candidate, factors: list) -> dict:
    """Structured evidence payload (IDEA: reason must be traceable)."""
    return {
        "rule_version": RULE_VERSION,
        "candidate": candidate.path,
        "factors": factors,
    }


def _factors_for(candidate: _Candidate, group: list[_Candidate]) -> list:
    """Compare one candidate against its group peers, per factor, in
    priority order. Each entry says who it beat / lost to, concretely."""
    factors = []
    peers = [c for c in group if c.key != candidate.key]
    if not peers:
        return factors
    if not candidate.readable:
        factors.append({"field": "readable", "value": False, "effect": "penalized"})
        return factors
    factors.append({"field": "readable", "value": True, "effect": "baseline"})
    # quality (worst normalized dimension; None loses to any measured value)
    others = [c for c in peers if c.quality_ok]
    if candidate.quality_ok:
        better = [c for c in others if (c.worst or 0.0) > (candidate.worst or 0.0)]
        worse = [c for c in others if (c.worst or 0.0) < (candidate.worst or 0.0)]
        if better:
            factors.append({
                "field": "quality(worst_dim)",
                "value": candidate.worst,
                "beaten_by": [c.path for c in better],
                "effect": "lost",
            })
        if worse:
            factors.append({
                "field": "quality(worst_dim)",
                "value": candidate.worst,
                "beats": [c.path for c in worse],
                "effect": "preferred",
            })
    else:
        factors.append({"field": "quality(worst_dim)", "value": None,
                        "effect": "unknown"})
    # resolution
    bigger = [c for c in peers if c.resolution > candidate.resolution]
    smaller = [c for c in peers if 0 < c.resolution < candidate.resolution]
    if bigger:
        factors.append({
            "field": "resolution",
            "value": f"{candidate.photo.get('width')}x{candidate.photo.get('height')}",
            "beaten_by": [c.path for c in bigger],
            "effect": "lost",
        })
    if smaller:
        factors.append({
            "field": "resolution",
            "value": f"{candidate.photo.get('width')}x{candidate.photo.get('height')}",
            "beats": [c.path for c in smaller],
            "effect": "preferred",
        })
    # file size (format preference: same scene, RAW/edited-JPG size is the
    # observable proxy — deliberately NOT "RAW always wins")
    bigger = [c for c in peers if c.size > candidate.size]
    if bigger:
        factors.append({
            "field": "file_size",
            "value": candidate.size,
            "format": candidate.photo.get("format"),
            "beaten_by": [c.path for c in bigger],
            "effect": "lost",
        })
    # metadata completeness
    richer = [c for c in peers if c.meta > candidate.meta]
    if richer:
        factors.append({
            "field": "metadata_completeness",
            "value": round(candidate.meta, 2),
            "beaten_by": [c.path for c in richer],
            "effect": "lost",
        })
    if not factors:
        factors.append({"field": "tie", "value": "all factors equal",
                        "effect": "stable_id"})
    return factors


def _rank_key(candidate: _Candidate):
    """Deterministic sort key (best first). Stable tie-break on rel_path,
    then photo_id, so results are reproducible across runs."""
    return (
        0 if candidate.readable else 1,
        -(candidate.worst if candidate.worst is not None else -1.0),
        -candidate.resolution,
        -candidate.size,
        -candidate.meta,
        candidate.path,
        candidate.key,
    )


def _decide_group(members: list[_Candidate]) -> dict:
    """Rank one group; the top candidate is the representative."""
    members.sort(key=_rank_key)
    rep = members[0]
    decisions = []
    for cand in members:
        is_rep = cand.key == rep.key
        factors = _factors_for(cand, members)
        factor_note = "; ".join(
            f"{f['field']}={f.get('value')}" for f in factors[:6]
        )
        reason = (
            f"[{RULE_VERSION}] representative of group: "
            f"readable; {factor_note}"
            if is_rep
            else f"[{RULE_VERSION}] loses to {rep.path}: {factor_note}"
        )
        decisions.append({
            "photo_id": cand.key,
            "path": cand.path,
            "status": POLICY_KEEP if is_rep else POLICY_DISCARD,
            "reason": reason,
            "evidence": _evidence(cand, factors),
            "final_score": None,
        })
    return {"representative": rep, "decisions": decisions}


def load_groups(conn: sqlite3.Connection, kinds: tuple = ("exact", "near")) -> list:
    """Load groups with their members' photos + quality rows."""
    kinds_sql = ",".join("?" for _ in kinds)
    rows = conn.execute(
        f"""
        SELECT g.group_id, g.kind, m.photo_id,
               p.rel_path, p.abs_path, p.size_bytes, p.width, p.height,
               p.format, p.taken_at, p.gps_lat, p.gps_lng,
               p.camera_make, p.camera_model,
               q.sharpness, q.exposure, q.noise
        FROM groups g
        JOIN group_members m ON m.group_id = g.group_id
        JOIN photos p ON p.photo_id = m.photo_id
        LEFT JOIN quality q ON q.photo_id = m.photo_id
        WHERE g.kind IN ({kinds_sql})
        ORDER BY g.group_id, m.photo_id
        """,
        tuple(kinds),
    ).fetchall()
    groups: dict[int, dict] = {}
    for row in rows:
        g = groups.setdefault(row[0], {"group_id": row[0], "kind": row[1],
                                       "members": []})
        photo = dict(zip(
            ("photo_id", "rel_path", "abs_path", "size_bytes", "width", "height",
             "format", "taken_at", "gps_lat", "gps_lng", "camera_make",
             "camera_model"),
            row[2:14],
        ))
        quality = {"sharpness": row[14], "exposure": row[15], "noise": row[16]}
        g["members"].append(_Candidate(photo, quality))
    return sorted(groups.values(), key=lambda g: g["group_id"])


def run_policy(conn: sqlite3.Connection, kinds: tuple = ("exact", "near")) -> dict:
    """Evaluate all groups and write policy recommendation rows.

    Only overwrites rows where source='policy'; user-confirmed rows
    (source='user') are left untouched, and photos the user already
    decided are excluded from being re-recommended.
    """
    # Drop previous policy rows for photos that are in a group.
    conn.execute(
        f"""
        DELETE FROM decisions
        WHERE source = 'policy'
          AND photo_id IN (
            SELECT photo_id FROM group_members
            WHERE group_id IN (
              SELECT group_id FROM groups WHERE kind IN ({','.join('?' for _ in kinds)})
            )
          )
        """,
        tuple(kinds),
    )
    groups = load_groups(conn, kinds)
    user_decided = {
        row[0]
        for row in conn.execute(
            "SELECT photo_id FROM decisions WHERE source = 'user'"
        )
    }
    stats = {"groups": 0, "keep": 0, "discard": 0, "user_ignored": 0}
    summary = []
    for group in groups:
        members = [
            c for c in group["members"] if c.key not in user_decided
        ]
        ignored = len(group["members"]) - len(members)
        stats["user_ignored"] += ignored
        if not members:
            continue
        outcome = _decide_group(members)
        for d in outcome["decisions"]:
            set_decision(
                conn,
                d["photo_id"],
                d["status"],
                final_score=d["final_score"],
                reason=d["reason"],
                source="policy",
                rule_version=RULE_VERSION,
            )
            stats["keep" if d["status"] == POLICY_KEEP else "discard"] += 1
        stats["groups"] += 1
        summary.append({
            "group_id": group["group_id"],
            "kind": group["kind"],
            "representative": outcome["representative"].path,
            "members": len(members),
            "user_ignored": ignored,
        })
    conn.commit()
    return {"stats": stats, "summary": summary}


def user_confirm(
    conn: sqlite3.Connection,
    group_id: int,
    keep_photo_id: int,
    others: tuple = ("DISCARD",),
) -> dict:
    """Record a user-confirmed decision for one group.

    ``others`` is a single status applied to every other member
    (default DISCARD; pass 'IGNORE' to keep undecided ones undecided).
    User rows have source='user' and are never overwritten by policy.
    """
    members = [
        row[0]
        for row in conn.execute(
            "SELECT photo_id FROM group_members WHERE group_id = ?", (group_id,)
        )
    ]
    if keep_photo_id not in members:
        raise ValueError(f"photo {keep_photo_id} is not in group {group_id}")
    group = conn.execute(
        "SELECT kind FROM groups WHERE group_id = ?", (group_id,)
    ).fetchone()
    if group is None:
        raise ValueError(f"group {group_id} not found")
    # keep member: always write a fresh user row (insert or overwrite the
    # policy row) so its reason reflects the confirmation, not the old
    # policy text.
    set_decision(
        conn, keep_photo_id, USER_KEEP,
        reason=(
            f"[{RULE_VERSION}] user confirmed representative of group {group_id}"
        ),
        source="user", rule_version=RULE_VERSION,
    )
    other_status = others[0]
    for photo_id in members:
        if photo_id == keep_photo_id:
            continue
        if other_status == "IGNORE":
            conn.execute(
                "DELETE FROM decisions WHERE photo_id=? AND source='policy'",
                (photo_id,),
            )
            continue
        set_decision(
            conn, photo_id, other_status,
            reason=(
                f"[{RULE_VERSION}] user confirmed {keep_photo_id} "
                f"({USER_KEEP}) for group {group_id}"
            ),
            source="user", rule_version=RULE_VERSION,
        )
    conn.commit()
    return {"group_id": group_id, "keep": keep_photo_id,
            "others": other_status, "kind": group[0]}
