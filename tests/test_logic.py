import random

import pytest

from bracketbot import logic


def test_normalize_name():
    assert logic.normalize_name("  Pizza   Party  ") == "Pizza Party"
    assert logic.normalize_name("tabs\tand\nnewlines") == "tabs and newlines"
    assert logic.normalize_name("") is None
    assert logic.normalize_name("   \t\n ") is None
    assert logic.normalize_name("​​") is None  # zero-width only
    assert logic.normalize_name("x" * 80) == "x" * 80
    assert logic.normalize_name("x" * 81) is None


def test_bracket_size():
    assert logic.bracket_size(2) == 2
    assert logic.bracket_size(3) == 4
    assert logic.bracket_size(5) == 8
    assert logic.bracket_size(8) == 8
    assert logic.bracket_size(33) == 64
    with pytest.raises(ValueError):
        logic.bracket_size(1)


@pytest.mark.parametrize("size", [2, 4, 8, 16, 32, 64])
def test_seed_order_is_standard(size):
    order = logic.seed_order(size)
    assert sorted(order) == list(range(1, size + 1))
    # Round-1 opponents always sum to size + 1 (1 vs size, 2 vs size-1, ...)
    for i in range(0, size, 2):
        assert order[i] + order[i + 1] == size + 1
    # Seeds 1 and 2 land in opposite halves, so they can only meet in the final
    half = size // 2
    assert (order.index(1) < half) != (order.index(2) < half)


@pytest.mark.parametrize("n", [2, 3, 5, 6, 7, 12, 32, 33])
def test_first_round_pairs_byes(n):
    ids = list(range(100, 100 + n))
    pairs = logic.first_round_pairs(ids)
    size = logic.bracket_size(n)
    assert len(pairs) == size // 2
    flat = [x for pair in pairs for x in pair]
    assert sorted(x for x in flat if x is not None) == sorted(ids)
    assert flat.count(None) == size - n
    # No bye-vs-bye, and byes go to the top seeds
    for a, b in pairs:
        assert a is not None or b is not None
        if b is None:
            assert a is not None
    bye_opponents = {a for a, b in pairs if b is None} | {b for a, b in pairs if a is None}
    top_seeds = set(ids[: size - n])
    assert bye_opponents == top_seeds


def test_decide():
    rng = random.Random(42)
    assert logic.decide(5, 3, rng) == ("a", "votes")
    assert logic.decide(0, 1, rng) == ("b", "votes")
    winner, decided_by = logic.decide(2, 2, rng)
    assert decided_by == "coinflip"
    assert winner in ("a", "b")
    winner, decided_by = logic.decide(0, 0, rng)  # 0-0 also coin-flips
    assert decided_by == "coinflip"
    # Coin flip is driven entirely by the injected RNG (deterministic here)
    assert [logic.decide(1, 1, random.Random(7))[0] for _ in range(3)] == [
        logic.decide(1, 1, random.Random(7))[0]
    ] * 3


def test_round_label():
    assert logic.round_label(1, 1) == "Final"
    assert logic.round_label(1, 2) == "Semifinals"
    assert logic.round_label(2, 2) == "Final"
    assert logic.round_label(1, 3) == "Quarterfinals"
    assert logic.round_label(1, 5) == "Round 1"
    assert logic.round_label(3, 5) == "Quarterfinals"


def test_truncate():
    assert logic.truncate("short", 80) == "short"
    assert logic.truncate("x" * 100, 80) == "x" * 79 + "…"
    assert len(logic.truncate("x" * 100, 80)) == 80
