"""Bot configuration helpers."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(slots=True)
class BotConfig:
    """Container for runtime configuration."""

    bot_token: str
    api_base_url: str
    admin_password: str | None = None
    webhook_url: str | None = None
    payments_key: str | None = None

    @classmethod
    def from_env(cls) -> "BotConfig":
        """Load configuration from environment variables."""
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN must be set")

        api_base_url = os.environ.get("LMS_API_BASE_URL", "http://localhost:5000")

        return cls(
            bot_token=token,
            api_base_url=api_base_url.rstrip("/"),
            admin_password=os.environ.get("LMS_ADMIN_PASSWORD"),
            webhook_url=os.environ.get("TELEGRAM_WEBHOOK_URL"),
            payments_key=os.environ.get("PAYMENTS_PROVIDER_KEY"),
        )
