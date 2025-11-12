"""Registration and onboarding flows."""

from __future__ import annotations

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters

from ..services.lms_api import LMSAPIError, LMSClient

ASK_NAME, ASK_WHATSAPP = range(2)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point that collects a player's details."""
    context.user_data.pop("registration", None)
    await _reply(update, "👋 Welcome to Last Man Standing! What's your full name?")
    return ASK_NAME


async def capture_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Persist the provided name and ask for WhatsApp."""
    message = update.message
    if not message:
        return ConversationHandler.END

    name = message.text.strip()
    if len(name) < 2:
        await message.reply_text("Please provide a valid name (at least 2 characters).")
        return ASK_NAME

    context.user_data["registration"] = {"name": name}
    await message.reply_text(
        "Great! If you'd like reminders, send your WhatsApp number (e.g. +441234567890).\n"
        "Otherwise, type /skip."
    )
    return ASK_WHATSAPP


async def capture_whatsapp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Finish registration once we have the optional WhatsApp number."""
    message = update.message
    if not message:
        return ConversationHandler.END

    whatsapp = message.text.strip()
    return await _complete_registration(update, context, whatsapp_number=whatsapp)


async def skip_whatsapp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Finish registration without a phone number."""
    return await _complete_registration(update, context, whatsapp_number=None)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Abort the registration flow."""
    await _reply(update, "Registration cancelled—feel free to /start again anytime.")
    context.user_data.pop("registration", None)
    return ConversationHandler.END


async def _complete_registration(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    whatsapp_number: str | None,
) -> int:
    message = update.message
    if not message:
        return ConversationHandler.END

    pending = context.user_data.get("registration") or {}
    name = pending.get("name")
    if not name:
        await message.reply_text("I lost your name. Please /start again.")
        return ConversationHandler.END

    lms_client = _get_lms_client(context)
    if not lms_client:
        await message.reply_text("Bot misconfigured: LMS client missing.")
        return ConversationHandler.END

    try:
        response = await lms_client.register_player(name=name, whatsapp_number=whatsapp_number)
    except LMSAPIError as exc:
        await message.reply_text(f"Registration failed: {exc}")
        return ConversationHandler.END

    success_msg = response.get("message") or f"Welcome {name}! You're now registered."
    await message.reply_text(success_msg)
    context.user_data.pop("registration", None)
    return ConversationHandler.END


def build_handler() -> ConversationHandler:
    """Construct the conversation handler for /start onboarding."""
    return ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, capture_name),
            ],
            ASK_WHATSAPP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, capture_whatsapp),
                CommandHandler("skip", skip_whatsapp),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        name="registration_conversation",
        persistent=False,
    )


async def _reply(update: Update, text: str) -> None:
    if update.message:
        await update.message.reply_text(text)


def _get_lms_client(context: ContextTypes.DEFAULT_TYPE) -> LMSClient | None:
    obj = context.application.bot_data.get("lms_client") if context.application else None
    return obj if isinstance(obj, LMSClient) else None
