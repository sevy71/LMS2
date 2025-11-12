"""Handlers for reminder notifications."""

from __future__ import annotations

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from ..services import LMSClient
from ..services.lms_api import LMSAPIError
from ..services.reminders import prepare_reminders


async def due_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a summary of reminders that need to go out."""
    message = update.message
    if not message:
        return

    lms = _get_lms_client(context)
    if not lms:
        await message.reply_text("Bot misconfigured: LMS client missing.")
        return

    try:
        payload = await lms.get_due_reminders()
    except LMSAPIError as exc:
        await message.reply_text(f"Could not fetch reminders: {exc}")
        return

    plan = prepare_reminders(payload)
    if not plan.reminders:
        await message.reply_text("🎉 No pending reminders right now.")
        return

    lines = ["📬 Reminders ready to send:"]
    limit = 5
    for reminder in plan.reminders[:limit]:
        player = reminder.get("player_name", "Unknown player")
        round_number = reminder.get("round_number", "?")
        reminder_type = reminder.get("reminder_type", "")
        reminder_id = reminder.get("reminder_id", "?")
        lines.append(f"- #{reminder_id} R{round_number} {player} ({reminder_type})")

    remaining = len(plan.reminders) - limit
    if remaining > 0:
        lines.append(f"...and {remaining} more. Use /reminders to refresh.")

    lines.append("Use /mark_sent <id> after you notify a player.")

    await message.reply_text("\n".join(lines))


async def mark_sent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mark a reminder as sent so it is not shown again."""
    message = update.message
    if not message:
        return
    if not context.args:
        await message.reply_text("Usage: /mark_sent <reminder_id>")
        return

    try:
        reminder_id = int(context.args[0])
    except ValueError:
        await message.reply_text("Reminder ID must be a number.")
        return

    lms = _get_lms_client(context)
    if not lms:
        await message.reply_text("Bot misconfigured: LMS client missing.")
        return

    try:
        await lms.mark_reminder_sent(reminder_id)
    except LMSAPIError as exc:
        await message.reply_text(f"Could not mark reminder: {exc}")
        return

    await message.reply_text(f"✅ Reminder {reminder_id} marked as sent.")


def build_handlers() -> list:
    """Handlers needed for reminder management."""
    return [
        CommandHandler("reminders", due_reminders),
        CommandHandler("mark_sent", mark_sent),
    ]


def _get_lms_client(context: ContextTypes.DEFAULT_TYPE) -> LMSClient | None:
    obj = context.application.bot_data.get("lms_client") if context.application else None
    return obj if isinstance(obj, LMSClient) else None
