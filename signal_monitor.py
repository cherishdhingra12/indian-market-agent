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

# Load .env for local credentials (before logging is set up)
try:
    from dotenv import load_dotenv
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    if os.path.exists(env_path):
        load_dotenv(env_path)
except Exception:
    pass

import requests

from alerts_db import init_db, was_notified, mark_notified, cleanup_old_entries, get_stats
from deep_sources import (
    FNO_STOCKS, NIFTY_50, BANK_NIFTY,
    get_fno_oi_snapshots,
    get_delivery_snapshots,
    scrape_india_vix,
    scrape_fii_dii_flows,
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
from predictor import run_predictor

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

    if signal.get("_format") == "predictive":
        msg = _format_predictive(signal)
    else:
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
    global _pred_cache
    try:
        from indian_market_agent import scrape_nse_announcements
        filings = scrape_nse_announcements()
    except Exception as e:
        log.error(f"Filing poll failed: {e}")
        return

    stock_universe = set(FNO_STOCKS)
    new_alerts = []
    for filing in filings:
        impact = classify_filing_impact(filing, stock_universe)
        if impact and impact.get("direction") != "neutral":
            new_alerts.append(impact)
            process_and_alert([impact], config)

    # Cache for predictor
    _pred_cache["filing_alerts"] = new_alerts

    log.info(f"Filing poll: {len(filings)} checked")


def poll_oi(config: dict):
    """Poll F&O OI data and detect buildups."""
    global _pred_cache
    try:
        current = get_fno_oi_snapshots()
    except Exception as e:
        log.error(f"OI poll failed: {e}")
        return

    if prev.oi:
        signals = check_oi_convergence(current, prev.oi)
        process_and_alert(signals, config)
        # Cache for predictor
        _pred_cache["oi_alerts"] = signals
    else:
        _pred_cache["oi_alerts"] = []

    _pred_cache["oi_data"] = current
    prev.oi = current
    log.info(f"OI poll: {len(current)} stocks, alerts sent")


def poll_deals(config: dict):
    """Poll bulk/block deals for F&O stocks."""
    global _pred_cache
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
    # Cache for predictor
    _pred_cache["deals"] = deals
    log.info(f"Deal poll: {len(alerts)} significant deals")


def poll_vix(config: dict):
    """Poll India VIX for fear/complacency."""
    global _pred_cache
    try:
        vix = scrape_india_vix()
        fii = scrape_fii_dii_flows()
        usd = scrape_usd_inr()
        signals = check_index_signals(vix_data=vix, fii_data=fii, usd_inr=usd)
        process_and_alert(signals, config)
        _pred_cache["index_signals"] = signals
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
    """Check delivery % from NSE bhavcopy (working via curl_cffi)."""
    global _pred_cache
    try:
        snapshots = get_delivery_snapshots()
        if not snapshots:
            return
        signals = check_delivery_spikes(snapshots)
        process_and_alert(signals, config)
        _pred_cache["delivery_snapshots"] = snapshots
        _pred_cache["delivery_alerts"] = signals
        log.info(f"Delivery poll: {len(signals)} signals from {len(snapshots)} stocks")
    except Exception as e:
        log.error(f"Delivery poll failed: {e}")


def poll_fno_ban(config: dict):
    """Check for new F&O ban entries."""
    global _pred_cache
    try:
        banned = scrape_fno_ban_list()
        signals = check_fno_ban(banned, prev.banned if prev.banned else None)
        process_and_alert(signals, config)
        _pred_cache["banned"] = banned
        prev.banned = banned
    except Exception as e:
        log.error(f"F&O ban poll failed: {e}")


# ─────────────────────────────────────────────────────────────────────
#  Predictive Signal Polling
# ─────────────────────────────────────────────────────────────────────

# Cache: latest data from each poll, fed to predictor for convergence
_pred_cache: dict = {
    "oi_alerts": [],
    "oi_data": {},
    "deals": {"bulk": [], "block": []},
    "filing_alerts": [],
    "index_signals": [],
    "banned": [],
    "delivery_alerts": [],
    "delivery_snapshots": {},
    "ai_insights": [],
}


def _format_predictive(signal: dict) -> str:
    """Format predictive ENTRY/EXIT/SURE_SHOT alerts with timing."""
    sig_type = signal.get("signal", "PREDICTIVE")
    action = signal.get("action", "")
    symbol = signal.get("symbol", "")
    confidence = signal.get("confidence", "MEDIUM")

    header_icon = "🚨" if sig_type == "SURE_SHOT" else ("🟢" if action == "ENTRY" else "🔴")
    header_label = "SURE SHOT" if sig_type == "SURE_SHOT" else f"PREDICTIVE {action}"

    lines = [
        f"{header_icon} <b>CONVERGENCE ALERT</b>",
        "═" * 35,
        f"<b>{symbol}</b> — {header_label}",
        f"Confidence: {'🔥' if confidence == 'HIGH' else '⚡'} {confidence}",
    ]

    reasons = signal.get("reasons", [])
    if reasons:
        lines.append("")
        for r in reasons[:4]:
            lines.append(f"▸ {r}")

    entry_hint = signal.get("entry_hint", "")
    if entry_hint:
        lines.append("")
        lines.append(entry_hint)

    exit_hint = signal.get("exit_hint", "")
    if exit_hint:
        lines.append("")
        lines.append(exit_hint)

    signal_types = signal.get("signal_types", [])
    lines.append("")
    lines.append(f"Sources: {', '.join(signal.get('sources', []))}")

    now = ist_now()
    lines.append(f"⏱ {now.strftime('%H:%M IST')}")

    return "\n".join(lines)


def poll_predictive(config: dict):
    """Run the predictive convergence engine on all cached data."""
    global _pred_cache
    data = _pred_cache

    oi_data = data.get("oi_data", {})
    deals = data.get("deals", {"bulk": [], "block": []})
    filing_alerts = data.get("filing_alerts", [])
    index_signals = data.get("index_signals", [])
    banned = data.get("banned", [])
    oi_alerts = data.get("oi_alerts", [])
    delivery_alerts = data.get("delivery_alerts", [])

    if not oi_data and not delivery_alerts:
        return

    result = run_predictor(
        oi_alerts=oi_alerts,
        deals=deals,
        filing_alerts=filing_alerts,
        index_signals=index_signals,
        oi_data=oi_data,
        banned_stocks=banned,
        delivery_alerts=delivery_alerts,
        universe=set(FNO_STOCKS),
    )

    # Send SURE_SHOT alerts first (highest priority)
    for alert in result.get("sure_shots", []):
        if was_notified(alert["symbol"], "SURE_SHOT", alert):
            continue
        success = send_predictive_alert(alert, config)
        if success:
            mark_notified(alert["symbol"], "SURE_SHOT", alert)
            time.sleep(1.5)

    # Then ENTRY alerts
    for alert in result.get("entries", []):
        if was_notified(alert["symbol"], "PREDICTIVE_ENTRY", alert):
            continue
        success = send_predictive_alert(alert, config)
        if success:
            mark_notified(alert["symbol"], "PREDICTIVE_ENTRY", alert)
            time.sleep(1.5)

    # Then EXIT alerts
    for alert in result.get("exits", []):
        if was_notified(alert["symbol"], "PREDICTIVE_EXIT", alert):
            continue
        success = send_predictive_alert(alert, config)
        if success:
            mark_notified(alert["symbol"], "PREDICTIVE_EXIT", alert)
            time.sleep(1.5)

    # Sector rotation (lower priority)
    for alert in result.get("sector_rotation", []):
        if was_notified(alert["symbol"], "SECTOR_ROTATION", alert):
            continue
        success = send_predictive_alert(alert, config)
        if success:
            mark_notified(alert["symbol"], "SECTOR_ROTATION", alert)
            time.sleep(1.5)

    log.info(
        f"Predictive: {len(result['entries'])} entries, "
        f"{len(result['exits'])} exits, "
        f"{len(result['sure_shots'])} sure shots, "
        f"{len(result['sector_rotation'])} sector rotations"
    )


def send_predictive_alert(signal: dict, config: dict) -> bool:
    """Send a predictive alert through the same Telegram bot as regular signals."""
    signal["_format"] = "predictive"
    return send_alert(signal, config)


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

                # Every 15 min — OI, deals, ban list, delivery + predictive convergence
                if minute % 15 == 0 and time.time() - last_oi_poll > 600:
                    log.info("--- OI/DEAL/BAN POLL ---")
                    poll_oi(config)
                    poll_deals(config)
                    poll_fno_ban(config)
                    poll_delivery(config)
                    poll_predictive(config)
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


def run_once(config: dict):
    """Execute ONE poll pass and exit — for stateless cloud (GitHub Actions) runs.

    Collapses the daemon's time-gated schedule into a single cycle: pre-market
    check when applicable, and the full market-hours poll set otherwise. Runs
    nothing (a clean no-op) outside market/pre-market windows so off-schedule
    cron fires are harmless. Dedup DB persistence between runs is handled by the
    caller (the workflow caches alerts.db).
    """
    log.info("=" * 60)
    log.info("NSE Signal Monitor — single pass (--once)")
    now = ist_now()
    log.info(f"IST {now.strftime('%Y-%m-%d %H:%M:%S')} | open={is_market_open()} "
             f"premarket={is_premarket()} postmarket={is_postmarket()}")

    init_db()
    cleanup_old_entries(days=3)

    if is_premarket():
        log.info("--- PRE-MARKET CHECK ---")
        poll_premarket(config)

    if is_market_open():
        # One full sweep. Order matters: OI/deals/delivery populate _pred_cache
        # that poll_predictive consumes; filings feed it too.
        poll_filings(config)
        log.info("--- OI/DEAL/BAN POLL ---")
        poll_oi(config)
        poll_deals(config)
        poll_fno_ban(config)
        poll_delivery(config)
        log.info("--- VIX/FII POLL ---")
        poll_vix(config)
        poll_predictive(config)
    elif not is_premarket():
        log.info("Outside market/pre-market window — nothing to poll.")

    stats = get_stats()
    log.info(f"Single pass complete — {stats['total_signals']} total alerts fired to date")


def check_kite(config: dict):
    """Connectivity self-test — verifies autonomous Kite auto-login + live OI,
    independent of market hours (quote() returns last-known OI even when closed).
    Prints a clear PASS/FAIL without ever exposing the token."""
    log.info("=" * 60)
    log.info("Kite connectivity self-test (--check)")
    try:
        import zerodha_source as kite
        if not kite.available():
            log.error("KITE CHECK: not configured / cannot obtain a token "
                      "(need KITE_API_KEY/SECRET + USER_ID/PASSWORD/TOTP_SECRET, "
                      "or a valid KITE_ACCESS_TOKEN)")
            return
        from deep_sources import FNO_STOCKS
        snaps = kite.get_oi_snapshots(list(FNO_STOCKS), indices=kite.INDEX_ROOTS)
        if snaps:
            sample = list(snaps)[:3]
            log.info(f"KITE CHECK: PASS — live OI for {len(snaps)} symbols. "
                     f"Sample: {', '.join(sample)}")
            for s in sample:
                d = snaps[s]
                log.info(f"    {s}: OI={d['total_oi']:,} price={d['underlying_value']}")
        else:
            log.error("KITE CHECK: token OK but no OI returned (check API "
                      "subscription / instruments)")
    except Exception as e:
        log.error(f"KITE CHECK: FAILED — {e}")


def main():
    config = load_config()
    if "--check" in sys.argv:
        check_kite(config)
    elif "--once" in sys.argv:
        run_once(config)
    else:
        main_loop(config)


if __name__ == "__main__":
    main()
