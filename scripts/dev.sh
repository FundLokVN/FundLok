#!/usr/bin/env bash
# `bun run dev`: the local sidecars are already up (docker compose); this
# starts the PartyKit dev server beside the API, then runs uvicorn in the
# foreground. PartyKit is stopped when uvicorn exits (Ctrl+C).
#
# PartyKit lives in the frontend repo (party/). It is skipped, with a note,
# when any of these holds -- the API still starts either way:
#   - PARTYKIT_HOST is not a local host (unset = feature off; a hosted
#     *.partykit.dev host means you are using the deployed one)
#   - the frontend repo's party/ folder is not found
#   - something already listens on :1999 (your own instance)
#
# Override the location with PARTYKIT_DIR=/path/to/Frontendfundlok/party.
set -uo pipefail
cd "$(dirname "$0")/.."

env_val() { grep -E "^$1=" .env 2>/dev/null | tail -n1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//'; }

PARTY_PID=""
cleanup() { [ -n "$PARTY_PID" ] && kill "$PARTY_PID" 2>/dev/null; }
trap cleanup EXIT INT TERM

HOST="$(env_val PARTYKIT_HOST)"
DIR="${PARTYKIT_DIR:-../Frontendfundlok/party}"
case "$HOST" in
  localhost:1999|127.0.0.1:1999)
    if [ ! -f "$DIR/partykit.json" ]; then
      echo "[partykit] skipped: $DIR not found (set PARTYKIT_DIR)"
    elif lsof -ti :1999 >/dev/null 2>&1; then
      echo "[partykit] skipped: something already listens on :1999"
    else
      [ -d "$DIR/node_modules" ] || (cd "$DIR" && npm install --no-audit --no-fund)
      JWT="$(env_val PARTYKIT_JWT_SECRET)"
      PUB="$(env_val PARTYKIT_PUBLISH_SECRET)"
      echo "[partykit] starting on :1999 (log: /tmp/partykit-dev.log)"
      (cd "$DIR" && exec npx partykit dev --port 1999 --no-hotkeys \
        --var "PARTYKIT_JWT_SECRET=$JWT" "PARTYKIT_PUBLISH_SECRET=$PUB") \
        >/tmp/partykit-dev.log 2>&1 &
      PARTY_PID=$!
    fi
    ;;
  *) echo "[partykit] off (PARTYKIT_HOST is not localhost:1999)" ;;
esac

./.venv/bin/uvicorn app.main:app --reload --port 8000 --no-proxy-headers
