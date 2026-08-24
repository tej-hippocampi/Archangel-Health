#!/usr/bin/env bash
# Local dev hub: brings up the whole product so a change can be clicked through
# as a real physician would meet it, not just asserted in a test.
#
#   ./scripts/dev-hub.sh          backend + landing
#   ./scripts/dev-hub.sh backend  backend only
#
# Email is forced into EMAIL_DEV_MODE, so every message the flow would send is
# printed to this terminal instead of being delivered. That is the point: the
# onboarding flow is mostly email, and you cannot review what you cannot see.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${1:-all}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
LANDING_PORT="${LANDING_PORT:-5173}"

PY="$ROOT/backend/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

# A dev .env, written once and then left alone. A developer's real keys must
# never be clobbered by running this script a second time.
if [ ! -f "$ROOT/backend/.env" ]; then
  echo "→ writing backend/.env for local dev (gitignored)"
  cat > "$ROOT/backend/.env" <<ENV
# Written by scripts/dev-hub.sh. Safe to edit; the script will not overwrite it.
EMAIL_DEV_MODE=1
BASE_URL=http://localhost:${BACKEND_PORT}
LANDING_URL=http://localhost:${LANDING_PORT}
AUTH_SECRET=dev-only-not-a-real-secret-$(date +%s)
ASCLEPIUS_AUTH_SECRET=dev-only-asclepius-secret-$(date +%s)
ASCLEPIUS_ADMIN_EMAIL=admin@localhost
ASCLEPIUS_ADMIN_PASSWORD=dev-admin-password
ADMIN_USERNAME=admin
ADMIN_PASSWORD=dev-admin-password
RATE_LIMIT_ENABLED=0
ENV
else
  echo "→ backend/.env already exists, leaving it alone"
fi

PIDS=()
cleanup() { for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null || true; done; }
trap cleanup EXIT INT TERM

echo "→ backend  http://localhost:${BACKEND_PORT}"
( cd "$ROOT/backend" && "$PY" -m uvicorn main:app --host 0.0.0.0 --port "$BACKEND_PORT" --reload ) &
PIDS+=($!)

if [ "$MODE" != "backend" ]; then
  if [ ! -d "$ROOT/landing/node_modules" ]; then
    echo "→ installing landing deps (first run only)"
    ( cd "$ROOT/landing" && npm install --silent )
  fi
  echo "→ landing  http://localhost:${LANDING_PORT}"
  ( cd "$ROOT/landing" && npm run dev -- --port "$LANDING_PORT" ) &
  PIDS+=($!)
fi

cat <<BANNER

  ────────────────────────────────────────────────────────────
   Archangel dev hub

   Landing            http://localhost:${LANDING_PORT}
   Asclepius portal   http://localhost:${BACKEND_PORT}/asclepius
   Admin console      http://localhost:${BACKEND_PORT}/admin.html
   API docs           http://localhost:${BACKEND_PORT}/docs

   Emails print to THIS terminal (EMAIL_DEV_MODE=1).
   To walk a physician signup, open the landing page and use
   "Become a contributor", or mint a link from the admin console.

   Email design preview, no server needed:
     backend/.venv/bin/python backend/scripts/email_preview.py --open
  ────────────────────────────────────────────────────────────

BANNER

wait
