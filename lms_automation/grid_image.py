"""Render the picks grid as an image for sharing in the WhatsApp group.

A cumulative grid — players down the side, rounds across the top, eliminated
players in red — is the shape the group already understands from other
sweepstakes. It does not survive as WhatsApp text: even two rounds wrap into
an unreadable mess on a phone, and a season reaches twenty.

The image is produced for the organiser to post. Posting to the group
automatically was tried and rejected: driving WhatsApp's search to open a
group chat raced with the keystrokes and sent a fragment of the search text
into the group.
"""

from __future__ import annotations

import os
from PIL import Image, ImageDraw, ImageFont

FONT_REGULAR = "/System/Library/Fonts/Supplemental/Arial.ttf"
FONT_BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"

# Deliberately high contrast: this gets viewed on a phone, often outdoors.
COL_HEADER_BG = (208, 208, 208)
COL_ELIMINATED = (218, 41, 28)      # red row, as in the group's other sweepstakes
COL_ELIMINATED_TEXT = (255, 255, 255)
COL_ROW_A = (255, 255, 255)
COL_ROW_B = (243, 243, 243)
COL_TEXT = (17, 17, 17)
COL_GRID = (176, 176, 176)
COL_WINNER = (214, 240, 214)        # survived this round

ROW_H = 30
NAME_W = 190
COL_W = 108
PAD = 10


def _short(team: str | None) -> str:
    """Compact a club name so a cell stays readable on a phone."""
    if not team:
        return ""
    name = team.strip()
    for suffix in (" FC", " AFC"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    name = name.replace("&", "and")
    swaps = {
        "Manchester United": "Man Utd",
        "Manchester City": "Man City",
        "Tottenham Hotspur": "Spurs",
        "Brighton and Hove Albion": "Brighton",
        "Nottingham Forest": "Forest",
        "Newcastle United": "Newcastle",
        "Wolverhampton Wanderers": "Wolves",
        "Crystal Palace": "Palace",
        "Leeds United": "Leeds",
        "West Ham United": "West Ham",
        "Sheffield United": "Sheff Utd",
        "Coventry City": "Coventry",
        "Ipswich Town": "Ipswich",
        "Hull City": "Hull",
        "Leicester City": "Leicester",
        "Aston Villa": "Villa",
    }
    return swaps.get(name, name)


def render_picks_grid(data: dict, out_path: str, title: str) -> str:
    """Draw the grid and return the path written."""
    rounds = data.get("rounds", [])
    players = sorted(data.get("players", []), key=lambda p: (p.get("name") or "").lower())

    width = NAME_W + COL_W * max(1, len(rounds)) + PAD * 2
    height = PAD * 2 + ROW_H * (len(players) + 2) + 26

    img = Image.new("RGB", (width, height), "white")
    d = ImageDraw.Draw(img)
    f_title = ImageFont.truetype(FONT_BOLD, 19)
    f_head = ImageFont.truetype(FONT_BOLD, 14)
    f_name = ImageFont.truetype(FONT_BOLD, 13)
    f_cell = ImageFont.truetype(FONT_REGULAR, 12)

    d.text((PAD, PAD), title, font=f_title, fill=COL_TEXT)
    top = PAD + 26

    # Header
    d.rectangle([PAD, top, width - PAD, top + ROW_H], fill=COL_HEADER_BG)
    d.text((PAD + 8, top + 8), "Player", font=f_head, fill=COL_TEXT)
    for i, rnd in enumerate(rounds):
        x = PAD + NAME_W + i * COL_W
        d.text((x + 8, top + 8), rnd.get("label", f"R{i+1}"), font=f_head, fill=COL_TEXT)

    # Rows
    for row, player in enumerate(players):
        y = top + ROW_H * (row + 1)
        eliminated = player.get("status") == "eliminated"
        bg = COL_ELIMINATED if eliminated else (COL_ROW_A if row % 2 == 0 else COL_ROW_B)
        text_col = COL_ELIMINATED_TEXT if eliminated else COL_TEXT

        d.rectangle([PAD, y, width - PAD, y + ROW_H], fill=bg)
        d.text((PAD + 8, y + 9), player.get("name", ""), font=f_name, fill=text_col)

        for i, rnd in enumerate(rounds):
            x = PAD + NAME_W + i * COL_W
            pick = (player.get("picks") or {}).get(rnd.get("round_key")) or {}
            team = _short(pick.get("team"))
            # Highlight a surviving pick, but never over a red row — the red
            # already carries the important information.
            if team and pick.get("is_winner") and not eliminated:
                d.rectangle([x, y, x + COL_W, y + ROW_H], fill=COL_WINNER)
            if team:
                d.text((x + 8, y + 10), team, font=f_cell, fill=text_col)

    # Grid lines last, so they sit above the fills
    bottom = top + ROW_H * (len(players) + 1)
    for row in range(len(players) + 2):
        yy = top + ROW_H * row
        d.line([PAD, yy, width - PAD, yy], fill=COL_GRID)
    for i in range(len(rounds) + 1):
        xx = PAD + NAME_W + i * COL_W
        d.line([xx, top, xx, bottom], fill=COL_GRID)
    d.line([PAD, top, PAD, bottom], fill=COL_GRID)
    d.line([width - PAD, top, width - PAD, bottom], fill=COL_GRID)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    img.save(out_path, "PNG")
    return out_path
