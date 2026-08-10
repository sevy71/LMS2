#!/bin/bash
# Launch wrapper for the LMS automation scheduler.
# Reads DATABASE_PUBLIC_URL and the sender token from .env; that file is
# gitignored and holds the live database credentials.
set -euo pipefail
cd "$(dirname "$0")"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

# The app reads DATABASE_URL; locally we point it at Railway's public endpoint.
export DATABASE_URL="${DATABASE_URL:-${DATABASE_PUBLIC_URL:-}}"

if [[ -z "${DATABASE_URL}" ]]; then
  echo "No DATABASE_URL or DATABASE_PUBLIC_URL in .env — refusing to start" >&2
  exit 1
fi

exec .venv/bin/python -m lms_automation.scheduler --interval "${TICK_MINUTES:-5}"
