"""Stage 2 (M1): near-duplicate candidate grouping with perceptual hashes.

Candidate rule: two photos are a near candidate when their pHash AND
dHash Hamming distances are BOTH within threshold (defined once in
``similarity.is_near_candidate``). Candidates are then linked with
union-find and each resulting connected component becomes a ``groups``
row with kind='near'.

Semantics (important):
- A near group is a connected-component candidate group, NOT a clique.
  Membership is transitive: A~B, B~C but A!~C still yields one group.
  The report must say so.
- Only the latest DB record per abs_path that still exists on disk
  participates: M0 keeps historical rows (old content at the same path)
  and rows for deleted files — neither may pollute M1 grouping.
- Exact SHA-256 duplicates are collapsed to one canonical row before
  pairing: byte-identical copies belong to the M0 exact stage, not
  near (repeating them in both sections would double-count in reports
  and in the M2 decision flow).
- Only hash rows written by the current algorithm version (default
  HASH_VERSION) participate; stale rows (e.g. NULL after an M0 -> M1
  upgrade) are ignored until the scanner recomputes them.

M1.0 writes candidates only — no ``decisions`` rows, no user-facing
verdict.

Scale boundary (M1.0): candidate pairing is O(N^2) pairwise comparison —
roughly N*(N-1)/2 comparisons (~50M at 10k photos, ~5B at 100k). This is
deliberate: M1.0 is meant to validate the hashes, thresholds, and
grouping logic on small/medium libraries. Do NOT scan a huge real
library with M1.0; a candidate-search index (LSH / bucketing / ANN) is
M1.1 work and must be benchmarked on real data before choosing.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from database import add_group, add_group_member, clear_near_results
from hashes import HASH_VERSION
from similarity import hamming_distance, is_near_candidate

# 64-bit hashes: max possible Hamming distance
_BIT_COUNT = 64


def _canonical_key(row):
    depth = row["rel_path"].count("/")
    return (depth, row["rel_path"], row["first_seen"] or "")


def build_near_groups(
    conn,
    phash_threshold: int = 8,
    dhash_threshold: int = 12,
    algorithm_version: str | None = None,
) -> dict:
    """Build near-candidate groups. Idempotent per run: existing 'near'
    groups are cleared first.

    Only rows meeting ALL of the following participate:
    - hash rows written by ``algorithm_version`` (default: current
      ``hashes.HASH_VERSION``), complete (both hashes non-NULL);
    - the latest photos record for each abs_path that still exists on
      disk (stale M0 history is ignored);
    - one canonical row per SHA-256 (exact copies are exact-stage only).
    """
    if algorithm_version is None:
        algorithm_version = HASH_VERSION

    clear_near_results(conn)
    rows = conn.execute(
        """
        SELECT p.photo_id, p.rel_path, p.abs_path, p.sha256, p.first_seen,
               h.phash, h.dhash
        FROM photo_hashes h
        JOIN photos p ON p.photo_id = h.photo_id
        WHERE h.algorithm_version = ?
          AND h.phash IS NOT NULL AND h.dhash IS NOT NULL
          AND p.photo_id = (
              SELECT MAX(p2.photo_id) FROM photos p2
              WHERE p2.abs_path = p.abs_path
          )
        ORDER BY p.photo_id
        """,
        (algorithm_version,),
    ).fetchall()

    # Rows whose file no longer exists on disk are stale: excluded.
    current = [row for row in rows if Path(row["abs_path"]).is_file()]

    # One canonical row per SHA-256: byte-identical copies belong to the
    # exact stage, not near.
    by_sha = {}
    for row in current:
        old = by_sha.get(row["sha256"])
        if old is None or _canonical_key(row) < _canonical_key(old):
            by_sha[row["sha256"]] = row
    rows = list(by_sha.values())
    rows.sort(key=lambda r: r["photo_id"])

    parent = {row["photo_id"]: row["photo_id"] for row in rows}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    n_pairs = 0
    for i in range(len(rows)):
        a = rows[i]
        for j in range(i + 1, len(rows)):
            b = rows[j]
            if is_near_candidate(
                a["phash"], b["phash"], a["dhash"], b["dhash"],
                phash_threshold, dhash_threshold,
            ):
                n_pairs += 1
                root_a, root_b = find(a["photo_id"]), find(b["photo_id"])
                if root_a != root_b:
                    parent[root_b] = root_a

    clusters: dict[int, list] = defaultdict(list)
    for row in rows:
        clusters[find(row["photo_id"])].append(row)

    n_groups = 0
    n_members = 0
    for members in clusters.values():
        if len(members) < 2:
            continue
        rep = min(members, key=_canonical_key)
        group_id = add_group(
            conn,
            kind="near",
            rep_photo_id=rep["photo_id"],
            size=len(members),
        )
        for member in members:
            add_group_member(
                conn,
                group_id,
                member["photo_id"],
                recall_source="phash+dhash",
                # Distance to the representative (not a fused similarity):
                # these are the exact numbers the candidate rule is built
                # on, so the report shows the evidence, not a fake score.
                sim_to_rep=1.0 - hamming_distance(
                    rep["phash"], member["phash"]
                ) / _BIT_COUNT,
            )
        n_groups += 1
        n_members += len(members) - 1

    conn.commit()
    return {
        "near_groups": n_groups,
        "near_pairs": n_pairs,
        "near_members": n_members,
        "phash_threshold": phash_threshold,
        "dhash_threshold": dhash_threshold,
    }
