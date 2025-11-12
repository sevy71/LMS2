"""Telegram bot package for the LMS migration."""

from .config import BotConfig
from .main import build_application

__all__ = ["BotConfig", "build_application"]
