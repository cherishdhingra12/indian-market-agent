#!/bin/bash
#===============================================================================
# Run the Indian Market News Agent
# Usage: ./run_agent.sh
#
# Designed to be called from cron. Sources .env file for API keys.
#===============================================================================
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

# Source .env file for API keys
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    source "$SCRIPT_DIR/.env"
    set +a
fi

PYTHON="python3"
if [ -f "$SCRIPT_DIR/venv/bin/python3" ]; then
    PYTHON="$SCRIPT_DIR/venv/bin/python3"
fi

mkdir -p "$SCRIPT_DIR/logs"
TIMESTAMP=$(date '+%Y-%m-%d_%H-%M-%S')
LOG_FILE="$SCRIPT_DIR/logs/run_$TIMESTAMP.log"

echo "========================================" | tee -a "$LOG_FILE"
echo "Market Agent Run: $(date)" | tee -a "$LOG_FILE"
echo "========================================" | tee -a "$LOG_FILE"

$PYTHON "$SCRIPT_DIR/indian_market_agent.py" "$@" 2>&1 | tee -a "$LOG_FILE"

EXIT_CODE=$?
echo "" | tee -a "$LOG_FILE"
echo "Exit code: $EXIT_CODE" | tee -a "$LOG_FILE"
echo "========================================" | tee -a "$LOG_FILE"
exit $EXIT_CODE
