from types import SimpleNamespace

import pytest

from bracketbot.views import VoteButton, vote_count_line, with_vote_count


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (0, "🗳️ **0 votes counted**"),
        (1, "🗳️ **1 vote counted**"),
        (2, "🗳️ **2 votes counted**"),
    ],
)
def test_vote_count_line_pluralizes(count, expected):
    assert vote_count_line(count) == expected


def test_with_vote_count_adds_and_replaces_total():
    matchup = "**Final — Matchup 1**\n**Pizza**  vs  **Tacos**"
    with_zero = with_vote_count(matchup, 0)
    assert with_zero == f"{matchup}\n🗳️ **0 votes counted**"
    assert with_vote_count(with_zero, 3) == f"{matchup}\n🗳️ **3 votes counted**"


async def test_vote_button_defers_privately_and_delegates():
    calls = []

    class Response:
        async def defer(self, **kwargs):
            calls.append(("defer", kwargs))

    class Cog:
        async def handle_vote(self, interaction, match_id, choice):
            calls.append(("handle", interaction, match_id, choice))

    cog = Cog()
    interaction = SimpleNamespace(
        response=Response(),
        client=SimpleNamespace(get_cog=lambda name: cog if name == "bracket" else None),
    )

    await VoteButton(42, "b").callback(interaction)

    assert calls == [
        ("defer", {"ephemeral": True}),
        ("handle", interaction, 42, "b"),
    ]
