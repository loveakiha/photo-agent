"""M1.1 v1 equivalence: the int-based early-exit pairing must be bit-exactly
identical to the M1.0 per-comparison hex-parsing candidate rule.

The whole point of M1.1 is "cheap speedup with zero recall loss". These
tests pin that down: for the same rows and thresholds, the new
``_pair_clusters`` (pre-parsed 64-bit ints + pHash-early-exit) produces the
exact same candidate pair count and the exact same connected components as
a reference implementation that calls ``similarity.is_near_candidate`` for
every pair (the M1.0 semantics).
"""
from __future__ import annotations

import random

from near import _pair_clusters
from similarity import is_near_candidate


def _reference_pairs_and_clusters(rows, phash_threshold, dhash_threshold):
    """M1.0 semantics, verbatim: per-pair is_near_candidate + union-find."""
    parent = {r["photo_id"]: r["photo_id"] for r in rows}

    def find(x):
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
                ra, rb = find(a["photo_id"]), find(b["photo_id"])
                if ra != rb:
                    parent[rb] = ra

    clusters = {}
    for row in rows:
        clusters.setdefault(find(row["photo_id"]), []).append(row["photo_id"])
    # canonical: frozenset of member ids per root
    canonical = {frozenset(v) for v in clusters.values()}
    return n_pairs, canonical


def _random_rows(n: int, seed: int) -> list[dict]:
    """n rows with random 16-hex hashes; photo_id distinct and ordered."""
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        rows.append(
            {
                "photo_id": i + 1,
                "rel_path": f"img{i:04d}.jpg",
                "phash": f"{rng.getrandbits(64):016x}",
                "dhash": f"{rng.getrandbits(64):016x}",
            }
        )
    return rows


def test_equivalence_random_hashes():
    """Over a random hash set, every threshold combo gives identical pairs
    and clusters to the M1.0 reference."""
    rows = _random_rows(120, seed=20260921)
    for phash_t in (4, 6, 8, 10, 12):
        for dhash_t in (8, 10, 12, 14, 16):
            ref_pairs, ref_clusters = _reference_pairs_and_clusters(
                rows, phash_t, dhash_t
            )
            n, clusters = _pair_clusters(
                rows,
                [int(r["phash"], 16) for r in rows],
                [int(r["dhash"], 16) for r in rows],
                phash_t,
                dhash_t,
            )
            assert n == ref_pairs, (
                f"pair count mismatch at pHash<={phash_t} dHash<={dhash_t}: "
                f"new={n} ref={ref_pairs}"
            )
            got = {frozenset(m["photo_id"] for m in v) for v in clusters.values()}
            assert got == ref_clusters, (
                f"cluster mismatch at pHash<={phash_t} dHash<={dhash_t}"
            )


def test_equivalence_chain_boundary():
    """The documented connected-component case: A~B, B~C but A!~C. The
    early-exit must still group all three (recall cannot silently drop the
    boundary pair B~C, whose pHash distance equals the threshold exactly)."""
    A = "0000000000000000"
    B = "0000000000000001"  # 1 bit from A
    C = "00000000000001ff"  # 8 bits from B (= threshold), 9 from A
    rows = [
        {"photo_id": 1, "rel_path": "a.jpg", "phash": A, "dhash": A},
        {"photo_id": 2, "rel_path": "b.jpg", "phash": B, "dhash": A},
        {"photo_id": 3, "rel_path": "c.jpg", "phash": C, "dhash": A},
    ]
    ref_pairs, ref_clusters = _reference_pairs_and_clusters(rows, 8, 12)
    n, clusters = _pair_clusters(
        rows,
        [int(r["phash"], 16) for r in rows],
        [int(r["dhash"], 16) for r in rows],
        8,
        12,
    )
    assert n == ref_pairs == 2
    got = {frozenset(m["photo_id"] for m in v) for v in clusters.values()}
    assert got == ref_clusters
    # The three land in exactly one component (A-B-C connected via B~C).
    assert {1, 2, 3} in got


def test_equivalence_empty_and_single():
    """Degenerate inputs: zero and one row -> no pairs, no groups."""
    for rows in ([], _random_rows(1, seed=1)):
        for t in (8, 12):
            n, clusters = _pair_clusters(
                rows,
                [int(r["phash"], 16) for r in rows],
                [int(r["dhash"], 16) for r in rows],
                t,
                t,
            )
            assert n == 0
            assert all(len(v) < 2 for v in clusters.values())


def test_equivalence_dhash_only_differs():
    """A pair whose pHash distance already exceeds the threshold but whose
    dHash is tiny must NOT be a candidate (early-exit skips it), matching the
    reference (both hashes must qualify)."""
    rows = [
        # pHash differ by 40 bits (> 8), dHash identical.
        {"photo_id": 1, "rel_path": "a.jpg", "phash": "0000000000000000", "dhash": "0000000000000000"},
        {"photo_id": 2, "rel_path": "b.jpg", "phash": "ffffffffffffff00", "dhash": "0000000000000000"},
    ]
    ref_pairs, _ = _reference_pairs_and_clusters(rows, 8, 12)
    n, _ = _pair_clusters(
        rows,
        [int(r["phash"], 16) for r in rows],
        [int(r["dhash"], 16) for r in rows],
        8,
        12,
    )
    assert n == ref_pairs == 0
