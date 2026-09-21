"""M1: compare perceptual hashes with Hamming distance.

Two 64-bit hex hashes differ by ``bit_count(a ^ b)`` bits. A small distance
means the two images are perceptually close. The threshold for a "near
duplicate" verdict is an experiment, so callers pass it in instead of baking
it in here.
"""
from __future__ import annotations


# Code-level defaults for the near candidate rule. These are the fallback
# when ``config.yaml`` has no ``near.phash`` / ``near.dhash`` section and the
# CLI does not override. The effective default for a run is read from the
# config (see ``cli.near_thresholds``).
DEFAULT_PHASH_THRESHOLD = 8
DEFAULT_DHASH_THRESHOLD = 12


def hamming_distance(hash_a: str, hash_b: str) -> int:
    """Number of differing bits between two hex hashes."""
    return (int(hash_a, 16) ^ int(hash_b, 16)).bit_count()


def is_near_candidate(
    phash_a: str,
    phash_b: str,
    dhash_a: str,
    dhash_b: str,
    phash_threshold: int = 8,
    dhash_threshold: int = 12,
) -> bool:
    """M1.0 candidate rule: both pHash and dHash must be close enough."""
    return (
        hamming_distance(phash_a, phash_b) <= phash_threshold
        and hamming_distance(dhash_a, dhash_b) <= dhash_threshold
    )
