#!/usr/bin/env python3
"""
Real-Time NSE Signal Monitor.
Continuous daemon that polls NSE/deep sources for market-moving signals
and fires instant Telegram alerts.

Architecture:
  - Polls NSE filings every 5 min
  - Polls F&O OI every 15 min
  - Polls bulk/block deals every 15 min
  - Polls VIX / gifts every 30 min
  - Checks delivery/FII at market close
  - Alerts ONLY when signal triggers (no scheduled digests)
"""

import json
import logging
import os
import sys
import time
import signal
from datetime import datetime, date, timezone, timedelta
from typing import List, Dict, Optional, Set

import requests

from alerts_db import init_db, was_notified, mark_notified, cleanup_old_entries, get_stats
from deep_sources import (
    FNO_STOCKS, NIFTY_50, BANK_NIFTY,
    get_fno_oi_snapshots,
    scrape_india_vix,
    scrape_gift_nifty,
    scrape_usd_inr,
    get_bulk_block_deals,
    scrape_fno_ban_list,
    ist_now, is_market_open, is_premarket, is_postmarket,
)
from signal_rules import (
    check_oi_convergence, check_delivery_spikes,
    check_multi_convergence, check_index_signals,
    classify_filing_impact, check_fno_ban,
    HIGH_IMPACT_FILING_CATS,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "signal_monitor.log")),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# Populate the FNO_STOCKS set in signal_rules for ban detection
import signal_rules
signal_rules.FNO_STOCKS = set(FNO_STOCKS)

# ─────────────────────────────────────────────────────────────────────
#  Configuration
# ─────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    """Load configuration from config.py."""
    try:
        sys.path.insert(0, BASE_DIR)
        import config as cfg
        return {
            "telegram_bot_token": getattr(cfg, "TELEGRAM_BOT_TOKEN", ""),
            "telegram_chat_id": getattr(cfg, "TELEGRAM_CHAT_ID", ""),
            "request_delay": getattr(cfg, "REQUEST_DELAY", 1.2),
            "llm_api_key": getattr(cfg, "LLM_API_KEY", ""),
        }
    except Exception as e:
        log.warning(f"Could not load config.py: {e}. Using env vars.")
        return {
            "telegram_bot_token": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""),
            "request_delay": 1.2,
            "llm_api_key": os.environ.get("GEMINI_API_KEY", ""),
        }


# ─────────────────────────────────────────────────────────────────────
#  Telegram Instant Alert
# ─────────────────────────────────────────────────────────────────────

def send_alert(signal: dict, config: dict) -> bool:
    """Send a single alert to Telegram immediately."""
    token = config.get("telegram_bot_token", "")
    chat_id = config.get("telegram_chat_id", "")
    if not token or "YOUR_" in token or not chat_id or "YOUR_" in chat_id:
        log.warning("Telegram not configured")
        return False

    msg = _format_alert(signal)
    if not msg:
        return False

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": msg,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        resp.raise_for_status()
        log.info(f"Alert sent: {signal.get('symbol', '')} — {signal.get('signal', '')}")
        return True
    except Exception as e:
        log.error(f"Telegram alert send failed: {e}")
        return False


def _format_alert(signal: dict) -> str:
    """Format a single signal into a compact Telegram message."""
    symbol = signal.get("symbol", "")
    sig_type = signal.get("signal", "")
    confidence = signal.get("confidence", "MEDIUM")
    reason = signal.get("reason", "")

    icon = "🔴"
    emoji_map = {
        "LONG_BUILDUP": "🟢",
        "SHORT_BUILDUP": "🔴",
        "LONG_UNWINDING": "🟡",
        "SHORT_COVERING": "🟢",
        "ACCUMULATION": "🟢",
        "DISTRIBUTION": "🔴",
        "DELIVERY_SURGE": "🟡",
        "CONVERGENCE": "🚨",
        "FILING_BULLISH": "📈",
        "FILING_BEARISH": "📉",
        "FILING_NEUTRAL": "📋",
        "VIX_SPIKE": "⚠️",
        "VIX_DROP": "✅",
        "FII_SELLING": "🔴",
        "DII_BUYING": "🟢",
        "PRE_MARKET_DOWN": "🔴",
        "PRE_MARKET_UP": "🟢",
        "FNO_BAN": "⛔",
    }
    icon = emoji_map.get(sig_type, "🔔")

    lines = [
        f"{icon} <b>{symbol}</b> — <b>{sig_type.replace('_', ' ')}</b>",
        "─" * 35,
    ]

    # Add signal-specific details
    oi_change = signal.get("oi_change_pct")
    price_change = signal.get("price_change_pct")
    delivery_pct = signal.get("delivery_pct")

    if oi_change is not None:
        lines.append(f"OI: {oi_change:+.1f}%  |  Price: {price_change:+.1f}%")
    if delivery_pct is not None:
        change = signal.get("change", 0)
        lines.append(f"Delivery: {delivery_pct}% ({change:+.1f}%)")

    # Confidence badge
    conf_icon = {"HIGH": "🔥", "MEDIUM": "⚡", "LOW": "💭"}
    lines.append(f"Confidence: {conf_icon.get(confidence, '')} {confidence}")

    lines.append("")
    lines.append(f"<i>{reason}</i>")

    now = ist_now()
    lines.append(f"\n⏱ {now.strftime('%H:%M IST')}")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────
#  Signal Router
# ─────────────────────────────────────────────────────────────────────

def process_and_alert(signals: List[Dict], config: dict):
    """
    Process a batch of signals through the alerting pipeline:
      1. Check dedup DB
      2. Attach timestamp
      3. Send to Telegram
      4. Mark as sent
    """
    for signal in signals:
        symbol = signal.get("symbol", "")
        sig_type = signal.get("signal", "")
        if not symbol or not sig_type:
            continue

        signal["_time"] = datetime.now(timezone.utc).isoformat()

        if was_notified(symbol, sig_type, signal):
            continue

        success = send_alert(signal, config)
        if success:
            mark_notified(symbol, sig_type, signal)
            time.sleep(1.5)  # rate limit between alerts


# ─────────────────────────────────────────────────────────────────────
#  Poller Functions
# ─────────────────────────────────────────────────────────────────────

class PreviousState:
    """Holds previous snapshots for comparison."""
    oi: Dict[str, Dict] = {}
    banned: List[str] = []


prev = PreviousState()


def poll_filings(config: dict):
    """Poll NSE corporate announcements for high-impact filings."""
    try:
        from indian_market_agent import scrape_nse_announcements
        filings = scrape_nse_announcements()
    except Exception as e:
        log.error(f"Filing poll failed: {e}")
        return

    stock_universe = set(FNO_STOCKS)
    for filing in filings:
        impact = classify_filing_impact(filing, stock_universe)
        if impact and impact.get("direction") != "neutral":
            process_and_alert([impact], config)

    log.info(f"Filing poll: {len(filings)} checked")


def poll_oi(config: dict):
    """Poll F&O OI data and detect buildups."""
    try:
        current = get_fno_oi_snapshots()
    except Exception as e:
        log.error(f"OI poll failed: {e}")
        return

    if prev.oi:
        signals = check_oi_convergence(current, prev.oi)
        process_and_alert(signals, config)

    prev.oi = current
    log.info(f"OI poll: {len(current)} stocks, alerts sent")


def poll_deals(config: dict):
    """Poll bulk/block deals for F&O stocks."""
    try:
        deals = get_bulk_block_deals()
    except Exception as e:
        log.error(f"Deal poll failed: {e}")
        return

    fno_set = set(FNO_STOCKS)
    alerts = []
    for deal in deals.get("bulk", []) + deals.get("block", []):
        symbol = deal.get("symbol", "").upper()
        if symbol not in fno_set:
            continue
        alerts.append({
            "symbol": symbol,
            "signal": f"{deal.get('deal_type', 'deal').upper()}_DEAL",
            "confidence": "HIGH",
            "reason": f"{deal.get('title', '')[:200]}",
        })

    process_and_alert(alerts, config)
    log.info(f"Deal poll: {len(alerts)} significant deals")


def poll_vix(config: dict):
    """Poll India VIX for fear/complacency."""
    try:
        vix = scrape_india_vix()
        fii = scrape_fii_dii_flows()
        usd = scrape_usd_inr()
        signals = check_index_signals(vix_data=vix, fii_data=fii, usd_inr=usd)
        process_and_alert(signals, config)
    except Exception as e:
        log.error(f"VIX/FII poll failed: {e}")


def poll_premarket(config: dict):
    """Check GIFT Nifty before market opens."""
    try:
        gift = scrape_gift_nifty()
        if gift:
            signals = check_index_signals(gift_nifty=gift)
            process_and_alert(signals, config)
    except Exception as e:
        log.error(f"Premarket poll failed: {e}")


def poll_delivery(config: dict):
    """Check delivery % at market close.
    Note: delivery data source is currently unavailable (NSE blocked quote-equity).
    This is a placeholder for when a working source becomes available.
    """
    pass


def poll_fno_ban(config: dict):
    """Check for new F&O ban entries."""
    try:
        banned = scrape_fno_ban_list()
        signals = check_fno_ban(banned, prev.banned if prev.banned else None)
        process_and_alert(signals, config)
        prev.banned = banned
    except Exception as e:
        log.error(f"F&O ban poll failed: {e}")


# ─────────────────────────────────────────────────────────────────────
#  Main Loop
# ─────────────────────────────────────────────────────────────────────

def main_loop(config: dict):
    """Main daemon loop with intelligent scheduling."""
    log.info("=" * 60)
    log.info("NSE Signal Monitor — Starting daemon")
    log.info(f"Monitoring {len(FNO_STOCKS)} F&O stocks")
    log.info("=" * 60)

    init_db()
    cleanup_old_entries(days=3)

    last_oi_poll = 0
    last_deals_poll = 0
    last_vix_poll = 0
    last_delivery_poll = 0
    last_ban_poll = 0
    last_premarket_poll = 0
    last_purge = time.time()
    poll_count = 0

    while True:
        try:
            now = ist_now()
            now_ts = time.time()
            minute = now.minute
            hour = now.hour
            weekday = now.weekday()
            market_open = is_market_open()
            premarket = is_premarket()
            postmarket = is_postmarket()

            # ── Premarket (8:00-9:15) ──
            if premarket and time.time() - last_premarket_poll > 300:
                log.info("--- PRE-MARKET CHECK ---")
                poll_premarket(config)
                last_premarket_poll = time.time()

            # ── Market Hours (9:15-15:30) ──
            if market_open:
                # Every 5 min — filings (always runs)
                poll_filings(config)
                poll_count += 1

                # Every 15 min — OI, deals, ban list
                if minute % 15 == 0 and time.time() - last_oi_poll > 600:
                    log.info("--- OI/DEAL/BAN POLL ---")
                    poll_oi(config)
                    poll_deals(config)
                    poll_fno_ban(config)
                    last_oi_poll = time.time()
                    last_deals_poll = time.time()
                    last_ban_poll = time.time()

                # Every 30 min — VIX, FII flows
                if minute % 30 == 0 and time.time() - last_vix_poll > 1200:
                    log.info("--- VIX/FII POLL ---")
                    poll_vix(config)
                    last_vix_poll = time.time()

                # At market close (~15:45) — delivery data
                if hour == 15 and minute >= 45 and time.time() - last_delivery_poll > 300:
                    log.info("--- MARKET CLOSE CHECK ---")
                    poll_delivery(config)
                    last_delivery_poll = time.time()

            # ── Weekend check ──
            if weekday >= 5:
                log.info("Weekend — sleeping 30 min")
                time.sleep(1800)
                continue

            # ── Daily cleanup ──
            if time.time() - last_purge > 86400:
                cleanup_old_entries(days=3)
                last_purge = time.time()

            # ── Sleep until next cycle ──
            sleep_time = 300 - (int(time.time()) % 300)  # align to 5-min boundary
            if sleep_time < 5:
                sleep_time = 300

            if not market_open and not premarket:
                sleep_time = 900 if postmarket else 600

            if poll_count % 12 == 0 and poll_count > 0:
                stats = get_stats()
                log.info(f"Monitor heartbeat — {stats['total_signals']} total alerts fired to date")

            time.sleep(min(sleep_time, 300))

        except KeyboardInterrupt:
            log.info("Monitor stopped by user")
            break
        except Exception as e:
            log.error(f"Main loop error: {e}", exc_info=True)
            time.sleep(60)


def main():
    config = load_config()
    main_loop(config)


if __name__ == "__main__":
    main()
