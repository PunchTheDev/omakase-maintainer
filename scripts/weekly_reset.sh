#!/usr/bin/env bash
# Weekly reset window (Mon 00:00 UTC). Cron:  0 0 * * 1  /path/to/weekly_reset.sh
# Pins the current OMK-R champion into OMK-H and re-baselines main, so the
# harness competition always builds on the latest winning router.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; [ -f .env.local ] && . ./.env.local; set +a
PY=../omakase-eval/.venv/bin/python
"$PY" -m omakase_maintainer.cli bump-pin --config configs/maintainer.dev.json
