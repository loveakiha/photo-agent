from __future__ import annotations

from similarity import hamming_distance, is_near_candidate


def test_identical_hashes_distance_zero():
    assert hamming_distance("ff00aa55ff00aa55", "ff00aa55ff00aa55") == 0


def test_single_bit_difference():
    assert hamming_distance("0000000000000001", "0000000000000000") == 1


def test_all_bits_differ():
    assert hamming_distance("f" * 16, "0" * 16) == 64


def test_candidate_rule_requires_both_hashes_close():
    assert is_near_candidate(
        "0000000000000000", "0000000000000001",
        "0000000000000000", "0000000000000001",
    )
    assert not is_near_candidate(
        "0" * 16, "f" * 8, "0" * 16, "f" * 8,
    )
