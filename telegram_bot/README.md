# Telegram Bot Scaffold

This directory houses the new Telegram automation layer that will sit alongside the existing Flask LMS. The goal is to keep LMS business logic in the current application while letting the bot act as another client (chat-first UX, payments, reminders).

## Layout

- `bot/` – Python package for runtime code (config, entrypoint, handlers, services).
- `tests/` – Space for unit tests as features get implemented.
- `requirements.txt` – Minimal dependencies for the bot service.
- `.env.example` – Document required environment variables for local runs.

## Current Capabilities

- `/start` conversation collects name + optional WhatsApp number and calls the legacy `/api/register` endpoint.
- `/pick <token>` stores a pick token and (once the LMS API exists) will display inline team options for submission. Until then it shares the web link fallback.
- `/reminders` fetches the legacy “due reminders” feed and summarises the first few pending notifications; `/mark_sent <id>` closes them out.

All commands share the existing Flask LMS via `LMSClient`, so no legacy files were modified.

## Next Steps

1. Install dependencies into your virtualenv: `pip install -r requirements.txt`.
2. Copy `.env.example` to `.env`, add Telegram bot token and LMS URLs.
3. Implement real pick-option/pick-submit endpoints in the LMS so the `/pick` flow can fully automate selections.
4. Decide whether to run via webhooks (preferred for production) or polling (scaffold defaults to polling).
5. Add admin gating (allowed chat IDs, passphrases) before exposing reminder management in production.
