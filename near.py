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

Scale & M1.1 v1:
Candidate pairing is still O(N^2) pairwise, but each hash is parsed from
hex to a 64-bit int ONCE (not per comparison), and the pHash distance is
checked first as an early-exit so the dHash distance is only computed when
the pHash distance already qualifies. This is bit-exactly equivalent to
the M1.0 per-comparison hex parsing: the candidate pairs, the pair count,
and the connected components are identical (see
``tests/test_near_equivalence.py``). No new dependency; tens of thousands
of photos finish in a few minutes, which is acceptable for an occasional
batch job. The upgrade path, if a real library is still too slow, is numpy
vectorization (numpy is already a transitive dependency). Approximate
bucketing / LSH is deliberately NOT introduced now, because exact
bucketing can silently drop boundary candidates — that would violate the
recall-first rule (宁可多抓候选，不要静默漏掉候选).
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from database import add_group, add_group_member, clear_near_results
from hashes import HASH_VERSION
from similarity import hamming_distance

# 64-bit hashes: max possible Hamming distance
_BIT_COUNT = 64


def _canonical_key(row):
    depth = row["rel_path"].count("/")
    return (depth, row["rel_path"], row["first_seen"] or "")


def _fetch_rows(conn, algorithm_version):
    """Every photo eligible for near grouping, fully filtered and sorted.

    Returns a list of row dicts (photo_id, rel_path, abs_path, sha256,
    first_seen, phash, dhash) that have: a hash row under
    ``algorithm_version`` (both hashes present), are the latest record per
    abs_path, still exist on disk, and are collapsed to one row per
    SHA-256. Sorted by photo_id.
    """
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
    return rows


def _pair_clusters(rows, phash_ints, dhash_ints, phash_threshold, dhash_threshold):
    """Pure union-find over pre-parsed 64-bit int hashes.

    ``rows`` and the two int lists are index-aligned. Returns
    ``(n_pairs, clusters)`` where ``clusters`` maps a representative
    photo_id to its member rows. Bit-exactly equivalent to the M1.0
    per-pair ``similarity.is_near_candidate`` check: pHash is evaluated
    first as an early-exit, then dHash. Performs no DB access.
    """
    parent = {row["photo_id"]: row["photo_id"] for row in rows}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    n = len(rows)
    n_pairs = 0
    for i in range(n):
        pi = phash_ints[i]
        di = dhash_ints[i]
        for j in range(i + 1, n):
            # Early-exit: pHash too far -> the pair cannot be a candidate,
            # so skip the dHash distance entirely.
            if (pi ^ phash_ints[j]).bit_count() > phash_threshold:
                continue
            if (di ^ dhash_ints[j]).bit_count() > dhash_threshold:
                continue
            n_pairs += 1
            root_a = find(rows[i]["photo_id"])
            root_b = find(rows[j]["photo_id"])
            if root_a != root_b:
                parent[root_b] = root_a

    clusters = defaultdict(list)
    for row in rows:
        clusters[find(row["photo_id"])].append(row)
    return n_pairs, clusters


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

    rows = _fetch_rows(conn, algorithm_version)
    phash_ints = [int(row["phash"], 16) for row in rows]
    dhash_ints = [int(row["dhash"], 16) for row in rows]

    n_pairs, clusters = _pair_clusters(
        rows, phash_ints, dhash_ints, phash_threshold, dhash_threshold
    )

    clear_near_results(conn)
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
                sim_to_rep=1.0
                - hamming_distance(rep["phash"], member["phash"]) / _BIT_COUNT,
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


def threshold_groups(
    conn,
    phash_threshold: int,
    dhash_threshold: int,
    algorithm_version: str | None = None,
) -> list[list]:
    """Read-only: the in-memory near groups (member rows, size>=2) for a
    single threshold pair, without writing to the database. Used by
    ``cli sweep --sheets`` to render contact sheets from a candidate
    combination while leaving the DB's official groups untouched.

    Each group is a list of member row dicts sorted by rel_path; the list
    of groups is ordered by the first member's rel_path so output is
    deterministic.
    """
    if algorithm_version is None:
        algorithm_version = HASH_VERSION
    rows = _fetch_rows(conn, algorithm_version)
    phash_ints = [int(row["phash"], 16) for row in rows]
    dhash_ints = [int(row["dhash"], 16) for row in rows]
    _, clusters = _pair_clusters(
        rows, phash_ints, dhash_ints, phash_threshold, dhash_threshold
    )
    groups = []
    for members in clusters.values():
        if len(members) < 2:
            continue
        groups.append(sorted(members, key=lambda r: r["rel_path"]))
    groups.sort(key=lambda m: m[0]["rel_path"])
    return groups
