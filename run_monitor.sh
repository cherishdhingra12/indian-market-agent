#!/bin/bash
# =============================================================================
# NSE Signal Monitor — Continuous Daemon Launcher
# =============================================================================
# Runs the real-time signal monitor that polls NSE every 5 minutes
# and fires instant Telegram alerts when signals trigger.
#
# Usage:
#   ./run_monitor.sh              # Run in foreground (Ctrl+C to stop)
#   nohup ./run_monitor.sh &      # Run in background
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

# Load environment variables from .env if it exists
if [ -f .env ]; then
    echo "[monitor] Loading .env"
    set -a
    source .env
    set +a
fi

# Validate required credentials
if [ -z "$TELEGRAM_BOT_TOKEN" ] || [ "$TELEGRAM_BOT_TOKEN" = "your_bot_token_here" ]; then
    echo "[monitor] ERROR: TELEGRAM_BOT_TOKEN not set. Configure .env first."
    exit 1
fi

if [ -z "$TELEGRAM_CHAT_ID" ] || [ "$TELEGRAM_CHAT_ID" = "your_chat_id_here" ]; then
    echo "[monitor] ERROR: TELEGRAM_CHAT_ID not set. Configure .env first."
    exit 1
fi

# Logging
LOG_FILE="$SCRIPT_DIR/logs/monitor_console.log"
mkdir -p "$SCRIPT_DIR/logs"

echo "[monitor] Starting NSE Signal Monitor..."
echo "[monitor] Logs: $LOG_FILE"
echo "[monitor] PID: $$"
echo "[monitor] Time: $(date '+%Y-%m-%d %H:%M:%S IST')"
echo ""

# Check Python availability
if command -v python3 &> /dev/null; then
    PYTHON=python3
elif command -v python &> /dev/null; then
    PYTHON=python
else
    echo "[monitor] ERROR: Python not found"
    exit 1
fi

# Run the monitor (foreground)
$PYTHON signal_monitor.py 2>&1 | tee -a "$LOG_FILE"
