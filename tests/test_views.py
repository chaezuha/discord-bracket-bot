from types import SimpleNamespace

import pytest

from bracketbot.views import (
    CONFIRM_TIMEOUT_MESSAGE,
    ConfirmView,
    VoteButton,
    vote_count_line,
)


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


async def test_confirm_view_timeout_disables_buttons_and_explains():
    edits = []

    class Origin:
        async def edit_original_response(self, **kwargs):
            edits.append(kwargs)

    view = ConfirmView()
    view.origin = Origin()
    await view.on_timeout()

    assert all(child.disabled for child in view.children)
    assert edits == [{"content": CONFIRM_TIMEOUT_MESSAGE, "view": view}]
    assert view.value is None


async def test_confirm_view_timeout_without_origin_only_disables():
    view = ConfirmView()
    await view.on_timeout()
    assert all(child.disabled for child in view.children)


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
