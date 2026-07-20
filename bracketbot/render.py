"""Bracket image rendering with Pillow.

`render_bracket` is a pure function of persisted state -> PNG bytes, so it can
run in a worker thread and be unit-tested without Discord. Vote counts appear
only on decided matches, which is how "tallies stay hidden while a round is
open" reaches the image: open matches simply have no winner yet.
"""

from __future__ import annotations

import io
from collections.abc import Mapping, Sequence

from PIL import Image, ImageDraw, ImageFont

from . import logic
from .models import FINISHED, Bracket, Match

# Discord-adjacent dark theme
BG = "#313338"
CELL = "#2b2d31"
BORDER = "#1e1f22"
TEXT = "#f2f3f5"
DIM = "#80848e"
WIN = "#57f287"
LINE = "#4e5058"
GOLD = "#f0b232"

CELL_W = 250
CELL_H = 60
COL_GAP = 48
V_GAP = 22
MARGIN = 28
HEADER_H = 76

# Candidate fonts: DejaVu (installed in the Docker image), common macOS/Linux
# faces, then Pillow's built-in as a last resort. Coverage is Latin-ish;
# unsupported glyphs render as boxes (documented limitation).
_FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
]
_BOLD_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
]


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in _BOLD_PATHS if bold else _FONT_PATHS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def _fit(draw: ImageDraw.ImageDraw, text: str, font, max_width: float) -> str:
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1].rstrip()
    return text + "…"


def render_bracket(
    bracket: Bracket,
    items: Mapping[int, str],
    matches: Sequence[Match],
    votes: Mapping[int, tuple[int, int]],
) -> bytes:
    """Draw the whole single-elimination tree; returns PNG bytes."""
    rounds: dict[int, list[Match]] = {}
    for match in sorted(matches, key=lambda m: (m.round, m.slot)):
        rounds.setdefault(match.round, []).append(match)
    total_rounds = max(rounds) if rounds else 1
    first_round = rounds.get(1, [])
    # Rounds that don't exist yet aren't drawn, but labels ("Semifinals",
    # "Final") must count them, and the full depth comes from round 1's size.
    label_rounds = logic.round_count(2 * len(first_round)) if first_round else total_rounds
    pitch = CELL_H + V_GAP

    finished = bracket.status == FINISHED
    champion_w = CELL_W + COL_GAP if finished else 0
    width = 2 * MARGIN + total_rounds * CELL_W + (total_rounds - 1) * COL_GAP + champion_w
    height = HEADER_H + len(first_round) * pitch + MARGIN

    img = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(img)
    font = _load_font(15)
    font_bold = _load_font(15, bold=True)
    font_small = _load_font(13)
    font_title = _load_font(22, bold=True)

    title = _fit(draw, bracket.name, font_title, width - 2 * MARGIN)
    draw.text((MARGIN, 16), title, font=font_title, fill=TEXT)

    # Column x positions and per-match center y positions
    def col_x(round_no: int) -> int:
        return MARGIN + (round_no - 1) * (CELL_W + COL_GAP)

    centers: dict[tuple[int, int], float] = {}
    for i, match in enumerate(first_round):
        centers[(1, match.slot)] = HEADER_H + i * pitch + CELL_H / 2
    for round_no in range(2, total_rounds + 1):
        for match in rounds.get(round_no, []):
            child_a = centers.get((round_no - 1, match.slot * 2 - 1))
            child_b = centers.get((round_no - 1, match.slot * 2))
            if child_a is not None and child_b is not None:
                centers[(round_no, match.slot)] = (child_a + child_b) / 2

    # Column labels
    for round_no in range(1, total_rounds + 1):
        label = logic.round_label(round_no, label_rounds)
        x = col_x(round_no) + CELL_W / 2 - draw.textlength(label, font=font_small) / 2
        draw.text((x, HEADER_H - 22), label, font=font_small, fill=DIM)

    def draw_row(x: float, y: float, item_id: int | None, match: Match, side: str) -> None:
        if item_id is None:
            draw.text((x + 10, y + 6), "—", font=font, fill=DIM)
            return
        name = items.get(item_id, "?")
        decided = match.winner is not None
        won = match.winner == side
        color, row_font = TEXT, font
        if decided:
            color, row_font = (WIN, font_bold) if won else (DIM, font)
        count_w = 0.0
        if decided and match.decided_by in ("votes", "coinflip"):
            votes_a, votes_b = votes.get(match.id, (0, 0))
            count = str(votes_a if side == "a" else votes_b)
            count_w = draw.textlength(count, font=row_font) + 14
            draw.text((x + CELL_W - 10 - count_w + 14, y + 6), count, font=row_font, fill=color)
        text = _fit(draw, name, row_font, CELL_W - 20 - count_w)
        draw.text((x + 10, y + 6), text, font=row_font, fill=color)

    # Connectors first, cells on top
    for round_no in range(2, total_rounds + 1):
        for match in rounds.get(round_no, []):
            parent_cy = centers.get((round_no, match.slot))
            if parent_cy is None:
                continue
            x_from = col_x(round_no - 1) + CELL_W
            x_mid = x_from + COL_GAP / 2
            x_to = col_x(round_no)
            for child_slot in (match.slot * 2 - 1, match.slot * 2):
                child_cy = centers.get((round_no - 1, child_slot))
                if child_cy is None:
                    continue
                draw.line([(x_from, child_cy), (x_mid, child_cy)], fill=LINE, width=2)
                draw.line([(x_mid, child_cy), (x_mid, parent_cy)], fill=LINE, width=2)
            draw.line([(x_mid, parent_cy), (x_to, parent_cy)], fill=LINE, width=2)

    for round_no in range(1, total_rounds + 1):
        for match in rounds.get(round_no, []):
            cy = centers.get((round_no, match.slot))
            if cy is None:
                continue
            x, y = col_x(round_no), cy - CELL_H / 2
            draw.rounded_rectangle(
                [x, y, x + CELL_W, y + CELL_H], radius=8, fill=CELL, outline=BORDER
            )
            draw.line([(x + 8, cy), (x + CELL_W - 8, cy)], fill=BORDER, width=1)
            draw_row(x, y, match.item_a, match, "a")
            draw_row(x, y + CELL_H / 2, match.item_b, match, "b")

    if finished:
        final = rounds[total_rounds][0]
        champion = items.get(final.winner_item_id or -1, "?")
        cy = centers[(total_rounds, final.slot)]
        x = col_x(total_rounds) + CELL_W
        draw.line([(x, cy), (x + COL_GAP, cy)], fill=LINE, width=2)
        x += COL_GAP
        y = cy - CELL_H / 2
        draw.rounded_rectangle([x, y, x + CELL_W, y + CELL_H], radius=8, fill=CELL, outline=GOLD)
        draw.text((x + 10, y + 8), "Champion", font=font_small, fill=GOLD)
        draw.text(
            (x + 10, y + 28), _fit(draw, champion, font_bold, CELL_W - 20), font=font_bold, fill=WIN
        )

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
