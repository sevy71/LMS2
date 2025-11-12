"""Pick submission stubs."""

from __future__ import annotations

from typing import Iterable

from telegram import Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from ..keyboards import build_pick_keyboard
from ..services.lms_api import LMSAPIError, LMSClient


async def pick_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Entry point for /pick <token>."""
    message = update.message
    if not message:
        return

    if not context.args:
        await message.reply_text(
            "Usage: /pick <token>\n"
            "You can find your token in the reminder link or dashboard URL."
        )
        return

    pick_token = context.args[0].strip()
    context.user_data["active_pick_token"] = pick_token

    lms = _get_lms_client(context)
    if not lms:
        await message.reply_text("Bot misconfigured: LMS client missing.")
        return

    try:
        payload = await lms.get_pick_options(pick_token)
    except LMSAPIError as exc:
        await message.reply_text(str(exc))
        return

    teams = _extract_teams(payload)
    if not teams:
        await message.reply_text(
            "No pick options returned. Please use the web form for now:\n"
            f"{lms.base_url}/pick/{pick_token}"
        )
        return

    keyboard = build_pick_keyboard(teams)
    await message.reply_text(
        "Select the team you'd like to pick for this round:",
        reply_markup=keyboard,
    )


async def pick_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline button presses for pick selection."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    data = query.data or ""
    if not data.startswith("pick:"):
        return

    team_name = data.split(":", 1)[1]
    pick_token = context.user_data.get("active_pick_token")
    lms = _get_lms_client(context)
    if not pick_token or not lms:
        await query.edit_message_text("Missing pick context; please run /pick again.")
        return

    try:
        response = await lms.submit_pick(pick_token, team_name)
    except LMSAPIError as exc:
        await query.edit_message_text(f"Pick submission failed: {exc}")
        return

    confirmation = response.get("message") or f"Pick saved: {team_name}"
    await query.edit_message_text(confirmation)
    context.user_data.pop("active_pick_token", None)


def build_handlers() -> list:
    """Return handlers needed for pick flow."""
    return [
        CommandHandler("pick", pick_command),
        CallbackQueryHandler(pick_selection, pattern=r"^pick:"),
    ]


def _get_lms_client(context: ContextTypes.DEFAULT_TYPE) -> LMSClient | None:
    obj = context.application.bot_data.get("lms_client") if context.application else None
    return obj if isinstance(obj, LMSClient) else None


def _extract_teams(payload: dict) -> Iterable[str]:
    teams = payload.get("teams")
    if isinstance(teams, list):
        filtered = [team for team in teams if isinstance(team, str)]
        return filtered
    return []
