"""Inline keyboards for pick submission."""

from __future__ import annotations

from typing import Iterable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def build_pick_keyboard(teams: Iterable[str]) -> InlineKeyboardMarkup:
    """Render teams in a simple grid."""
    buttons = [
        InlineKeyboardButton(text=team, callback_data=f"pick:{team}")
        for team in teams
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(rows)
