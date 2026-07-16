#!/bin/bash
# scripts/run-full-pipeline.sh
#
# Wrapper script invoked by launchd at 06:00 daily. Runs the full agent
# pipeline (scrape → analyze → alerts → export-dashboard) with proper env
# setup, working directory, and logging.
#
# Why a wrapper: launchd jobs run with a near-empty environment — no PATH,
# no shell rc files sourced — so we explicitly source ~/.zshrc to pick up
# TELEGRAM_BOT_TOKEN and any other exported secrets, and cd to the repo
# so relative paths (DASHBOARD_REPO_PATH default, ./.venv, ./data) resolve.

# NOTE: deliberately NOT using `set -e` here.
#   ~/.zshrc commonly contains commands that exit non-zero (presence checks
#   for brew, nvm, conda, etc.). Sourcing it under `set -e` kills the whole
#   wrapper before anything useful runs — which is exactly what bit us when
#   first testing under launchd.
# We use `set -u` to catch unset variables (safe), and pipefail so failures
# inside a pipe propagate. Errors from individual commands are logged and we
# soldier on — the real pipeline (`python -m src.main full`) reports its own
# exit code which is what cron should care about.
set -u
set -o pipefail

REPO_DIR="/Users/kbez/Downloads/Personal/Claude/Projects/Sofia Real Estate/sofia-realestate-agent"
LOG_DIR="$REPO_DIR/data/logs"
LOG_FILE="$LOG_DIR/cron-$(date +%Y-%m-%d).log"

mkdir -p "$LOG_DIR"

# Source shell rc for env vars (TELEGRAM_BOT_TOKEN etc).
# Disable `set -u` and `set -o pipefail` around the source: ~/.zshrc commonly
# references unset variables and runs commands in pipes that exit non-zero.
# Without these guards, sourcing aborts the whole wrapper silently.
# We restore the strict modes after sourcing.
if [ -f "$HOME/.zshrc" ]; then
    set +u
    set +o pipefail
    # shellcheck disable=SC1090
    source "$HOME/.zshrc" 2>/dev/null || true
    set -u
    set -o pipefail
fi

cd "$REPO_DIR"

{
    echo "════════════════════════════════════════════════════════════"
    echo "Sofia RE pipeline — $(date '+%Y-%m-%d %H:%M:%S %Z')"
    echo "════════════════════════════════════════════════════════════"

    # Use the project venv if present; fall back to system python.
    if [ -x "$REPO_DIR/.venv/bin/python" ]; then
        PY="$REPO_DIR/.venv/bin/python"
    else
        PY="$(command -v python3)"
    fi
    echo "Python: $PY"

    # ── Watchdog (TIN-517) ────────────────────────────────────────────────
    # The 2026-07-13 run hung for 48+ hours and silently blocked the next
    # two nightly launches. Hard rule: if the pipeline exceeds
    # MAX_PIPELINE_HOURS (default 3), kill it, log loudly, and exit non-zero
    # so tomorrow's launchd run starts from a clean slate no matter what.
    MAX_PIPELINE_HOURS="${MAX_PIPELINE_HOURS:-3}"
    MAX_PIPELINE_SECS=$(( MAX_PIPELINE_HOURS * 3600 ))

    "$PY" -m src.main full &
    PIPELINE_PID=$!

    (
        sleep "$MAX_PIPELINE_SECS"
        if kill -0 "$PIPELINE_PID" 2>/dev/null; then
            echo "WATCHDOG: pipeline exceeded ${MAX_PIPELINE_HOURS}h — killing PID $PIPELINE_PID at $(date '+%Y-%m-%d %H:%M:%S %Z')"
            kill -TERM "$PIPELINE_PID" 2>/dev/null
            sleep 30
            kill -KILL "$PIPELINE_PID" 2>/dev/null || true
        fi
    ) &
    WATCHDOG_PID=$!

    # (script intentionally runs without `set -e` — see header)
    wait "$PIPELINE_PID"
    PIPELINE_RC=$?
    # Pipeline done (or killed) — retire the watchdog subshell.
    kill "$WATCHDOG_PID" 2>/dev/null || true

    if [ "$PIPELINE_RC" -ne 0 ]; then
        echo "Pipeline exited with code $PIPELINE_RC at $(date '+%Y-%m-%d %H:%M:%S %Z')"
    fi

    echo
    echo "Pipeline finished at $(date '+%Y-%m-%d %H:%M:%S %Z')"
    exit "$PIPELINE_RC"
} >> "$LOG_FILE" 2>&1
