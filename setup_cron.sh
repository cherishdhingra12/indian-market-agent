#!/bin/bash
#===============================================================================
# Setup cron jobs for Indian Market News Agent
#
# Two deployment options:
#   1. LOCAL: cron jobs on your Mac/Linux (runs when computer is on)
#   2. CLOUD: GitHub Actions (free, 24/7) — recommended
#
# For GitHub Actions: Push to GitHub and add secrets there.
#   See .github/workflows/market_agent.yml for details.
#===============================================================================
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RUNNER="$SCRIPT_DIR/run_agent.sh"
CRON_LOG="$SCRIPT_DIR/logs/cron.log"

chmod +x "$RUNNER" "$SCRIPT_DIR/indian_market_agent.py"
mkdir -p "$SCRIPT_DIR/logs"

# Install Python deps if needed
echo "Checking Python dependencies..."
python3 -c "import requests; import bs4" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "Installing required Python packages..."
    pip3 install -r "$SCRIPT_DIR/requirements.txt" 2>&1 || {
        echo "ERROR: Failed to install dependencies."
        exit 1
    }
fi

# Check config for placeholder values
if [ -f "$SCRIPT_DIR/config.py" ]; then
    if grep -q "YOUR_" "$SCRIPT_DIR/config.py" 2>/dev/null; then
        echo ""
        echo "WARNING: config.py has placeholder values."
        echo ""
        echo "Option 1: Edit config.py directly"
        echo "  ${EDITOR:-nano} $SCRIPT_DIR/config.py"
        echo ""
        echo "Option 2: Use .env file (recommended for GitHub Actions)"
        echo "  cp $SCRIPT_DIR/.env.example $SCRIPT_DIR/.env"
        echo "  ${EDITOR:-nano} $SCRIPT_DIR/.env"
        echo ""
        echo "Option 3: Use GitHub Actions (FREE cloud deployment)"
        echo "  1. Push this repo to GitHub"
        echo "  2. Go to Settings > Secrets and Variables > Actions"
        echo "  3. Add these secrets:"
        echo "     - GEMINI_API_KEY"
        echo "     - TELEGRAM_BOT_TOKEN"
        echo "     - TELEGRAM_CHAT_ID"
        echo "  4. The workflow in .github/workflows/ runs automatically"
        echo ""
        ${EDITOR:-nano} "$SCRIPT_DIR/config.py"
    fi
fi

echo ""
echo "=== Setup Complete ==="
echo ""
echo "LOCAL DEPLOYMENT (cron):"
echo "  Run: crontab -e"
echo "  Add these lines (adjust Python path if needed):"
echo "  # 9:00 AM IST"
echo "  30 3 * * * $RUNNER >> $CRON_LOG 2>&1"
echo "  # 2:00 PM IST"
echo "  30 8 * * * $RUNNER >> $CRON_LOG 2>&1"
echo "  # 7:00 PM IST"
echo "  30 13 * * * $RUNNER >> $CRON_LOG 2>&1"
echo ""
echo "CLOUD DEPLOYMENT (FREE — RECOMMENDED):"
echo "  1. Create a GitHub repo and push this code"
echo "  2. Add secrets in Settings > Secrets > Actions:"
echo "     - GEMINI_API_KEY"
echo "     - TELEGRAM_BOT_TOKEN"
echo "     - TELEGRAM_CHAT_ID"
echo "  3. The workflow runs automatically at 9AM/2PM/7PM IST"
echo ""
echo "TEST IMMEDIATELY:"
echo "  $RUNNER"
echo ""
echo "Logs: $CRON_LOG"
echo "Done."
