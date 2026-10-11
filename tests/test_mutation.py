from __future__ import annotations

from cybergym_harness.mutation import generate_mutations


def test_mutations_are_deterministic_and_shifted():
    data = bytes(range(32))
    first = generate_mutations(data, max_mutations=6, seed=7)
    second = generate_mutations(data, max_mutations=6, seed=7)
    assert [b for b, _ in first] == [b for b, _ in second]
    assert len(first) == 6
    base = set(range(32))
    for blob, note in first:
        assert len(blob) == len(data)
        assert blob != data
        assert note
        diff = [index for index in base if blob[index] != data[index]]
        assert diff
        assert max(diff) >= 21


def test_mutations_empty_and_disabled():
    assert generate_mutations(b"", max_mutations=4) == []
    assert generate_mutations(b"abcd", max_mutations=0) == []
