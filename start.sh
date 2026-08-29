#!/usr/bin/env bash
# Start adderall, pulling the latest code first.
#
#   ./start.sh              run in the foreground (Ctrl-C to stop)
#   ./start.sh -d           run detached, in the background
#
# Any arguments are passed through to `docker compose up`.
#
# If you run without Docker, the equivalent is:
#   git pull && uvicorn app.main:app --host 0.0.0.0 --port 8000

set -uo pipefail
cd "$(dirname "$0")"

if [ -d .git ]; then
    echo "→ checking for updates…"
    # --ff-only: never invent a merge commit. If the pull can't fast-forward
    # (local edits, diverged history), say so and keep running what's here
    # rather than refusing to start.
    if ! git pull --ff-only; then
        echo "⚠  Could not update (local changes or diverged history)." >&2
        echo "   Starting the version you already have." >&2
    fi
else
    echo "⚠  Not a git checkout — skipping the update." >&2
fi

# --build matters: the image bakes in the app code, so without it Docker
# happily serves the previous build and updates appear to do nothing.
echo "→ building and starting…"
exec docker compose up --build "$@"
