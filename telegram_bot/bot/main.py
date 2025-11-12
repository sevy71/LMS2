"""Entrypoint for the Telegram bot service."""

from __future__ import annotations

import logging

from dotenv import load_dotenv
from telegram.ext import Application, ApplicationBuilder

from .config import BotConfig
from .handlers import picks, registration, reminders
from .services import LMSClient

logger = logging.getLogger(__name__)


def build_application(config: BotConfig) -> Application:
    """Construct the telegram.ext Application with all handlers."""
    app = ApplicationBuilder().token(config.bot_token).build()
    app.bot_data["config"] = config
    app.bot_data["lms_client"] = LMSClient(
        base_url=config.api_base_url,
        admin_password=config.admin_password,
    )

    # Register handlers.
    app.add_handler(registration.build_handler())
    for handler in picks.build_handlers():
        app.add_handler(handler)
    for handler in reminders.build_handlers():
        app.add_handler(handler)

    return app


def main() -> None:
    """CLI hook: load config and start polling."""
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    config = BotConfig.from_env()
    application = build_application(config)

    if config.webhook_url:
        logger.info("Webhook mode not implemented yet; falling back to polling.")

    application.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
