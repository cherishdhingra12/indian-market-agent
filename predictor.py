"""
Deep Predictive Signal Engine — Indian Stock Market

Cross-references ALL available data sources to detect high-probability
ENTRY and EXIT signals with confidence scoring and timing.

Sources consumed:
  - F&O OI changes (OI buildup/unwinding per stock)
  - Bulk/block deals (smart money movement)
  - NSE corporate filings (insider trades, board outcomes, order wins)
  - SEBI orders (regulatory actions)
  - India VIX (market fear/greed)
  - GIFT Nifty (pre-market indication)
  - F&O ban list (MWPL concentration)
  - USD/INR (currency impact)

Signal types produced:
  - PREDICTIVE_ENTRY:  2+ bullish signals on same stock → BUY with target
  - PREDICTIVE_EXIT:   2+ bearish signals on same stock → SELL/AVOID
  - OI_CONCENTRATION:  Abnormal OI in specific strikes (whale watching)
  - SECTOR_ROTATION:   Capital flowing between sectors
  - SURE_SHOT:         3+ convergent signals → highest conviction alert
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional, Tuple, Set
from collections import defaultdict

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────
# Confidence & Threshold Configuration
# ─────────────────────────────────────────────────────────────────────

ENTRY_CONFIDENCE_THRESHOLD = 2    # signals needed for ENTRY alert
EXIT_CONFIDENCE_THRESHOLD = 2     # signals needed for EXIT alert
SURE_SHOT_THRESHOLD = 3           # signals needed for SURE_SHOT alert

# Deal aggregation: a symbol's bulk/block deals are collapsed into ONE net
# directional signal. Two-sided churn (e.g. 8 BUY + 8 SELL on the same
# small-cap) is NOT a directional signal and must be dropped — otherwise it
# trips both ENTRY and EXIT and manufactures contradictory SURE_SHOTs.
DEAL_MIN_ROWS = 2                 # need at least this many deal rows to judge
DEAL_DOMINANCE = 0.60            # dominant side must be >=60% of rows

# Tradable universe the predictor is allowed to emit alerts for. Populated by
# run_predictor(); defaults to the Nifty-50 set below. Symbols outside this set
# (illiquid small-caps surfacing via bulk deals) are ignored.
TRACKED_UNIVERSE: set = set()

NIFTY_50_SYMBOLS = {
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "HINDUNILVR", "ICICIBANK",
    "ITC", "SBIN", "BHARTIARTL", "KOTAKBANK", "LT", "WIPRO",
    "AXISBANK", "BAJFINANCE", "ASIANPAINT", "MARUTI", "SUNPHARMA",
    "TATAMOTORS", "NTPC", "M&M", "TITAN", "JSWSTEEL", "POWERGRID",
    "NESTLEIND", "TECHM", "HCLTECH", "BAJAJFINSV", "ULTRACEMCO",
    "SBILIFE", "TATASTEEL", "GRASIM", "CIPLA", "DABUR", "HINDALCO",
    "ONGC", "ADANIPORTS", "DRREDDY", "EICHERMOT", "INDUSINDBK",
    "BAJAJAUTO", "DIVISLAB", "BRITANNIA", "COALINDIA", "BPCL",
    "SHRIRAMFIN", "ADANIENT", "HEROMOTOCO", "HDFCLIFE", "APOLLOHOSP",
}

# Sector mapping for rotation detection
SECTOR_MAP = {
    "RELIANCE": "OIL_GAS", "ONGC": "OIL_GAS", "BPCL": "OIL_GAS", "COALINDIA": "OIL_GAS",
    "TCS": "IT", "INFY": "IT", "WIPRO": "IT", "TECHM": "IT", "HCLTECH": "IT",
    "HDFCBANK": "BANKING", "ICICIBANK": "BANKING", "SBIN": "BANKING", "KOTAKBANK": "BANKING",
    "AXISBANK": "BANKING", "INDUSINDBK": "BANKING", "BANDHANBNK": "BANKING",
    "BAJFINANCE": "FINANCE", "BAJAJFINSV": "FINANCE", "SHRIRAMFIN": "FINANCE",
    "LT": "INFRA", "ULTRACEMCO": "INFRA", "GRASIM": "INFRA",
    "TATAMOTORS": "AUTO", "M&M": "AUTO", "MARUTI": "AUTO", "BAJAJAUTO": "AUTO", "EICHERMOT": "AUTO", "HEROMOTOCO": "AUTO",
    "SUNPHARMA": "PHARMA", "CIPLA": "PHARMA", "DRREDDY": "PHARMA", "DIVISLAB": "PHARMA", "TORPHARMA": "PHARMA",
    "HINDUNILVR": "FMCG", "ITC": "FMCG", "NESTLEIND": "FMCG", "DABUR": "FMCG", "BRITANNIA": "FMCG", "MARICO": "FMCG", "TATACONSUM": "FMCG",
    "TITAN": "CONSUMER", "DMART": "CONSUMER", "TRENT": "CONSUMER", "HAVELLS": "CONSUMER",
    "TATASTEEL": "METALS", "JSWSTEEL": "METALS", "HINDALCO": "METALS",
    "ADANIPORTS": "INFRA", "ADANIENT": "CONGLOMERATE", "ADANIGREEN": "ENERGY", "ADANITRANS": "ENERGY",
    "NTPC": "POWER", "POWERGRID": "POWER",
    "BHARTIARTL": "TELECOM",
    "SBILIFE": "INSURANCE", "HDFCLIFE": "INSURANCE", "ICICIPRULI": "INSURANCE",
    "SBICARD": "FINANCE",
    "ASIANPAINT": "CONSUMER", "BERGEPAINT": "CONSUMER", "PIDILITIND": "CONSUMER",
    "APOLLOHOSP": "HEALTHCARE",
}

# Stock-specific price ranges for entry/exit levels
STOCK_PRICE_RANGES = {
    "RELIANCE": (2500, 3200), "TCS": (3500, 4500), "HDFCBANK": (1500, 2000),
    "INFY": (1400, 1800), "ICICIBANK": (1100, 1400), "SBIN": (700, 950),
    "BHARTIARTL": (1200, 1600), "LT": (3400, 4200), "TATAMOTORS": (800, 1200),
    "M&M": (2500, 3200), "TITAN": (3000, 3800), "MARUTI": (10000, 13000),
    "AXISBANK": (1100, 1400), "ITC": (400, 550), "SUNPHARMA": (1500, 2000),
    "KOTAKBANK": (1600, 2100), "BAJFINANCE": (6500, 8500), "NTPC": (300, 450),
    "JSWSTEEL": (800, 1100), "WIPRO": (400, 600), "HINDUNILVR": (2200, 3000),
    "TATASTEEL": (100, 180), "ADANIPORTS": (1200, 1600), "POWERGRID": (280, 380),
    "NESTLEIND": (2200, 2800), "ONGC": (240, 340), "ULTRACEMCO": (10000, 13000),
}


# ─────────────────────────────────────────────────────────────────────
#  Signal Buffer — stores recent signal history for convergence analysis
# ─────────────────────────────────────────────────────────────────────

_classified_signals: Dict[str, List[Dict]] = defaultdict(list)
_sector_buys: Dict[str, int] = defaultdict(int)
_sector_sells: Dict[str, int] = defaultdict(int)
_oi_concentration: Dict[str, Dict] = {}

SIGNAL_TTL_SECONDS = 3600  # signals expire after 1 hour


def _get_sector(symbol: str) -> str:
    return SECTOR_MAP.get(symbol, "OTHER")


def _is_nifty50(symbol: str) -> bool:
    return symbol in NIFTY_50_SYMBOLS


def _expired_signals(symbol: str) -> List[Dict]:
    now = datetime.now(timezone.utc).timestamp()
    active = []
    for s in _classified_signals.get(symbol, []):
        if now - s.get("_ts", 0) < SIGNAL_TTL_SECONDS:
            active.append(s)
    _classified_signals[symbol] = active
    return active


# ─────────────────────────────────────────────────────────────────────
#  Signal Classification
# ─────────────────────────────────────────────────────────────────────

def _classify_oi_signal(signal: Dict) -> Optional[str]:
    stype = signal.get("signal", "")
    if stype in ("LONG_BUILDUP", "SHORT_COVERING", "ACCUMULATION", "DELIVERY_SURGE"):
        return "BULLISH"
    elif stype in ("SHORT_BUILDUP", "LONG_UNWINDING", "DISTRIBUTION"):
        return "BEARISH"
    return None


def _classify_deal(deal: Dict) -> Optional[str]:
    title = (deal.get("title", "") + " " + deal.get("deal_type", "")).lower()
    snippet = deal.get("snippet", "").lower()
    combined = title + " " + snippet
    if any(kw in combined for kw in ["buy", "purchase", "acquirer", "acquisition"]):
        return "BULLISH"
    if any(kw in combined for kw in ["sell", "sold", "seller", "sale"]):
        return "BEARISH"
    return None


def _in_universe(symbol: str) -> bool:
    """Only emit alerts for tracked (F&O/Nifty) symbols. If no universe was
    configured, fall back to the Nifty-50 set so we never alert on random
    small-caps surfaced by bulk deals."""
    universe = TRACKED_UNIVERSE or NIFTY_50_SYMBOLS
    return symbol in universe


def _net_deal_signals(deals: Dict[str, List[Dict]]) -> Dict[str, Dict]:
    """
    Collapse all bulk+block deal rows for each symbol into a single NET
    directional signal.

    A bulk/block deal is intrinsically two-sided (every buyer has a seller),
    and one stock can have many rows in a day. Counting each row as an
    independent bullish/bearish "signal" is invalid — it lets churn masquerade
    as convergence. Instead we tally BUY vs SELL rows and only produce a signal
    when one side clearly dominates.

    Returns {symbol: {"direction": "BULLISH"|"BEARISH", "buy": n, "sell": m}}.
    """
    tally: Dict[str, Dict[str, int]] = defaultdict(lambda: {"buy": 0, "sell": 0})
    for deal_type in ("bulk", "block"):
        for deal in deals.get(deal_type, []):
            sym = (deal.get("symbol") or "").upper()
            if not sym or not _in_universe(sym):
                continue
            side = _classify_deal(deal)
            if side == "BULLISH":
                tally[sym]["buy"] += 1
            elif side == "BEARISH":
                tally[sym]["sell"] += 1

    net: Dict[str, Dict] = {}
    for sym, t in tally.items():
        total = t["buy"] + t["sell"]
        if total < DEAL_MIN_ROWS:
            continue
        if t["buy"] >= t["sell"] and t["buy"] / total >= DEAL_DOMINANCE:
            net[sym] = {"direction": "BULLISH", "buy": t["buy"], "sell": t["sell"]}
        elif t["sell"] > t["buy"] and t["sell"] / total >= DEAL_DOMINANCE:
            net[sym] = {"direction": "BEARISH", "buy": t["buy"], "sell": t["sell"]}
        # else: two-sided churn → no directional signal
    return net


def _classify_filing(filing: Dict) -> Optional[str]:
    direction = filing.get("direction", "neutral")
    if direction == "bullish":
        return "BULLISH"
    elif direction == "bearish":
        return "BEARISH"
    return None


# ─────────────────────────────────────────────────────────────────────
#  OI Concentration Detection
# ─────────────────────────────────────────────────────────────────────

def check_oi_concentration(oi_data: Dict[str, Dict]) -> List[Dict]:
    """Detect abnormal OI buildup in specific strikes (whale activity)."""
    alerts = []
    for symbol, data in oi_data.items():
        total_oi = data.get("total_oi", 0) or 0
        ce_oi = data.get("total_ce_oi", 0) or 0
        pe_oi = data.get("total_pe_oi", 0) or 0
        pcr = data.get("put_call_ratio", 1) or 1
        oi_change = abs(data.get("total_oi_change", 0) or 0)

        if total_oi < 100000:  # skip illiquid
            continue

        pcr_low = pcr < 0.3
        pcr_high = pcr > 1.5
        abnormal_oi = oi_change > 500000  # 5L+ shares OI change

        if pcr_low and abnormal_oi:
            alerts.append({
                "symbol": symbol,
                "signal": "OI_CONCENTRATION",
                "confidence": "MEDIUM",
                "pcr": pcr,
                "total_oi": total_oi,
                "oi_change": oi_change,
                "direction": "CE_heavy",
                "reason": f"{symbol}: PCR {pcr} (extremely low) + OI change {oi_change:,} — Heavy call writing, bullish bias"
            })
        elif pcr_high and abnormal_oi:
            alerts.append({
                "symbol": symbol,
                "signal": "OI_CONCENTRATION",
                "confidence": "MEDIUM",
                "pcr": pcr,
                "total_oi": total_oi,
                "oi_change": oi_change,
                "direction": "PE_heavy",
                "reason": f"{symbol}: PCR {pcr} (extremely high) + OI change {oi_change:,} — Heavy put buying, bearish bias"
            })
    return alerts


# ─────────────────────────────────────────────────────────────────────
#  Predictive ENTRY Detection
# ─────────────────────────────────────────────────────────────────────

def check_predictive_entry(
    oi_alerts: List[Dict],
    deals: Dict[str, List[Dict]],
    filing_alerts: List[Dict],
    index_signals: List[Dict],
    oi_data: Dict[str, Dict],
) -> List[Dict]:
    """
    Detect ENTRY opportunities:
      Signal 1: OI LONG_BUILDUP + Price up
      Signal 2: Bulk deal BUY on same stock
      Signal 3: Bullish corporate filing
      Signal 4: OI concentration (CE heavy)
      Signal 5: Sector-level bullish rotation

    When >= 2 signals → ENTRY alert (confidence level depends on count)
    """
    alerts = []
    pooled = defaultdict(list)

    # Process OI signals
    for sig in oi_alerts:
        sym = sig["symbol"]
        if not _in_universe(sym):
            continue
        direction = _classify_oi_signal(sig)
        if direction:
            pooled[sym].append({"type": f"OI_{direction}", "data": sig, "source": "oi"})

    # Process deals — NET direction per symbol (one signal, not one per row)
    for sym, nd in _net_deal_signals(deals).items():
        pooled[sym].append({"type": f"DEAL_{nd['direction']}", "data": nd, "source": "deal"})

    # Process filing alerts
    for filing in filing_alerts:
        sym = filing.get("symbol", "")
        if not _in_universe(sym):
            continue
        direction = _classify_filing(filing)
        if direction:
            pooled[sym].append({"type": f"FILING_{direction}", "data": filing, "source": "filing"})

    # Process OI concentration
    conc_alerts = _oi_concentration.get("last", [])
    for conc in conc_alerts:
        sym = conc["symbol"]
        if not _in_universe(sym):
            continue
        if conc.get("direction") == "CE_heavy":
            pooled[sym].append({"type": "OI_CONCENTRATION_CE", "data": conc, "source": "concentration"})

    # Generate ENTRY signals
    for symbol, signals in pooled.items():
        bullish = [s for s in signals if "BULLISH" in s["type"] or "CE" in s["type"]]
        if len(bullish) >= ENTRY_CONFIDENCE_THRESHOLD:
            sources = list(set(s["source"] for s in bullish))
            reasons = []
            for s in bullish:
                d = s["data"]
                if s["source"] == "oi":
                    oi_pct = d.get('oi_change_pct', 0) or 0
                    price_pct = d.get('price_change_pct', 0) or 0
                    if oi_pct and price_pct:
                        reasons.append(f"OI +{oi_pct}%, Price +{price_pct}%")
                    elif 'delivery_pct' in d:
                        reasons.append(f"Delivery {d['delivery_pct']}% (+{d.get('change', 0):+.1f}%)")
                    else:
                        reasons.append(f"OI +{oi_pct}%, Price +{price_pct}%")
                elif s["source"] == "deal":
                    reasons.append(_net_deal_label(s["data"]))
                elif s["source"] == "filing":
                    reasons.append(f"{s['data'].get('category', 'filing')}")
                elif s["source"] == "concentration":
                    reasons.append(f"OI concentration (PCR {d.get('pcr', '?')})")

            price_range = STOCK_PRICE_RANGES.get(symbol, (0, 0))
            support, resistance = price_range or (0, 0)
            base_price = (bullish[0]["data"].get("underlying_value") or
                          bullish[0]["data"].get("last_price") or 0)

            nifty50_bonus = 2 if _is_nifty50(symbol) else 0
            confidence = "HIGH" if len(bullish) >= 3 else "MEDIUM"

            entry_hint = _generate_entry_hint(symbol, base_price, support, resistance, confidence)

            alerts.append({
                "symbol": symbol,
                "signal": "PREDICTIVE_ENTRY",
                "action": "ENTRY",
                "confidence": confidence,
                "signal_count": len(bullish),
                "signal_types": list(set(s["type"] for s in bullish)),
                "sources": sources,
                "reasons": reasons,
                "sector": _get_sector(symbol),
                "is_nifty50": _is_nifty50(symbol),
                "entry_hint": entry_hint,
                "reason": f"ENTRY: {len(bullish)} bullish signals on {symbol} — {' | '.join(reasons[:3])}",
            })

    return alerts


# ─────────────────────────────────────────────────────────────────────
#  Predictive EXIT Detection
# ─────────────────────────────────────────────────────────────────────

def check_predictive_exit(
    oi_alerts: List[Dict],
    deals: Dict[str, List[Dict]],
    filing_alerts: List[Dict],
    index_signals: List[Dict],
    oi_data: Dict[str, Dict],
) -> List[Dict]:
    """
    Detect EXIT / AVOID opportunities:
      Signal 1: OI SHORT_BUILDUP or LONG_UNWINDING
      Signal 2: Bulk deal SELL on same stock
      Signal 3: Bearish corporate filing (CEO resignation, downgrade)
      Signal 4: OI concentration (PE heavy)
      Signal 5: Stock banned in F&O

    When >= 2 signals → EXIT alert
    """
    alerts = []
    pooled = defaultdict(list)

    for sig in oi_alerts:
        sym = sig["symbol"]
        if not _in_universe(sym):
            continue
        direction = _classify_oi_signal(sig)
        if direction:
            pooled[sym].append({"type": f"OI_{direction}", "data": sig, "source": "oi"})

    # Deals — NET direction per symbol (one signal, not one per row)
    for sym, nd in _net_deal_signals(deals).items():
        pooled[sym].append({"type": f"DEAL_{nd['direction']}", "data": nd, "source": "deal"})

    for filing in filing_alerts:
        sym = filing.get("symbol", "")
        if not _in_universe(sym):
            continue
        direction = _classify_filing(filing)
        if direction:
            pooled[sym].append({"type": f"FILING_{direction}", "data": filing, "source": "filing"})

    conc_alerts = _oi_concentration.get("last", [])
    for conc in conc_alerts:
        sym = conc["symbol"]
        if not _in_universe(sym):
            continue
        if conc.get("direction") == "PE_heavy":
            pooled[sym].append({"type": "OI_CONCENTRATION_PE", "data": conc, "source": "concentration"})

    # Check F&O ban
    banned_set = _oi_concentration.get("banned", set())

    for symbol, signals in pooled.items():
        bearish = [s for s in signals if "BEARISH" in s["type"] or "PE" in s["type"]]
        banned_bonus = symbol in banned_set

        if len(bearish) >= EXIT_CONFIDENCE_THRESHOLD or banned_bonus:
            if banned_bonus:
                bearish.append({"type": "FNO_BAN", "source": "ban", "data": {}})

            reasons = []
            for s in bearish:
                d = s["data"]
                if s["source"] == "oi":
                    oi_pct = d.get('oi_change_pct', 0) or 0
                    price_pct = d.get('price_change_pct', 0) or 0
                    if oi_pct and price_pct:
                        reasons.append(f"OI {oi_pct}%, Price {price_pct}%")
                    elif 'delivery_pct' in d:
                        reasons.append(f"Delivery {d['delivery_pct']}% ({d.get('change', 0):+.1f}%)")
                    else:
                        reasons.append(f"OI {oi_pct}%, Price {price_pct}%")
                elif s["source"] == "deal":
                    reasons.append(_net_deal_label(s["data"]))
                elif s["source"] == "filing":
                    reasons.append(f"{s['data'].get('category', 'filing')}")
                elif s["source"] == "concentration":
                    reasons.append(f"PE concentration (PCR {d.get('pcr', '?')})")
                elif s["source"] == "ban":
                    reasons.append("F&O Ban (MWPL > 95%)")

            confidence = "HIGH" if len(bearish) >= 3 else "MEDIUM"
            exit_hint = _generate_exit_hint(symbol)

            alerts.append({
                "symbol": symbol,
                "signal": "PREDICTIVE_EXIT",
                "action": "EXIT",
                "confidence": confidence,
                "signal_count": len(bearish),
                "signal_types": list(set(s["type"] for s in bearish)),
                "sources": list(set(s["source"] for s in bearish)),
                "reasons": reasons,
                "sector": _get_sector(symbol),
                "is_nifty50": _is_nifty50(symbol),
                "exit_hint": exit_hint,
                "reason": f"EXIT: {len(bearish)} bearish signals on {symbol} — {' | '.join(reasons[:3])}",
            })

    return alerts


# ─────────────────────────────────────────────────────────────────────
#  SURE_SHOT Detection (3+ convergent signals)
# ─────────────────────────────────────────────────────────────────────

def check_sure_shot(
    entry_alerts: List[Dict],
    exit_alerts: List[Dict],
) -> List[Dict]:
    """Filter for highest-conviction signals where 3+ sources converge."""
    sure_shots = []
    for alert in entry_alerts + exit_alerts:
        if alert.get("signal_count", 0) >= SURE_SHOT_THRESHOLD:
            sure_shots.append({
                **alert,
                "signal": "SURE_SHOT",
                "confidence": "HIGH",
                "reason": f"🚨 SURE SHOT: {alert['action']} {alert['symbol']} — {alert['signal_count']} sources converged {' | '.join(alert.get('reasons', [])[:3])}",
            })
    return sure_shots


# ─────────────────────────────────────────────────────────────────────
#  Sector Rotation Detection
# ─────────────────────────────────────────────────────────────────────

def check_sector_rotation(
    entry_alerts: List[Dict],
    exit_alerts: List[Dict],
) -> List[Dict]:
    """Detect capital flowing between sectors based on signal clustering."""
    alerts = []
    sector_entries = defaultdict(list)
    sector_exits = defaultdict(list)

    for a in entry_alerts:
        sec = a.get("sector", "OTHER")
        if sec != "OTHER":
            sector_entries[sec].append(a)

    for a in exit_alerts:
        sec = a.get("sector", "OTHER")
        if sec != "OTHER":
            sector_exits[sec].append(a)

    for sector, entries in sector_entries.items():
        if len(entries) >= 2:
            symbols = [e["symbol"] for e in entries]
            alerts.append({
                "symbol": f"SECTOR:{sector}",
                "signal": "SECTOR_ROTATION",
                "confidence": "MEDIUM",
                "action": "INFLOW",
                "sector": sector,
                "stock_count": len(entries),
                "stocks": symbols,
                "reason": f"Sector rotation INFLOW: {sector} — {len(entries)} stocks showing ENTRY signals ({', '.join(symbols[:5])})",
            })

    for sector, exits in sector_exits.items():
        if len(exits) >= 2:
            symbols = [e["symbol"] for e in exits]
            alerts.append({
                "symbol": f"SECTOR:{sector}",
                "signal": "SECTOR_ROTATION",
                "confidence": "MEDIUM",
                "action": "OUTFLOW",
                "sector": sector,
                "stock_count": len(exits),
                "stocks": symbols,
                "reason": f"Sector rotation OUTFLOW: {sector} — {len(exits)} stocks showing EXIT signals ({', '.join(symbols[:5])})",
            })

    return alerts


# ─────────────────────────────────────────────────────────────────────
#  Entry/Exit Timing Helpers
# ─────────────────────────────────────────────────────────────────────

def _generate_entry_hint(symbol: str, base_price: float, support: float, resistance: float, confidence: str) -> str:
    now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    market_hours = 9 <= now.hour < 15 or (now.hour == 15 and now.minute <= 30)
    timing = "IMMEDIATE" if market_hours else "PRE-MARKET TOMORROW"

    if base_price <= 0:
        return f"{timing}: Monitor for entry. Check price before acting."

    target = base_price * 1.05
    stoploss = base_price * 0.97

    return (
        f"🕐 Timing: {timing}\n"
        f"💰 Entry Zone: ₹{base_price:,.0f} - ₹{base_price * 1.01:,.0f}\n"
        f"🎯 Target 1: ₹{base_price * 1.03:,.0f} (+3%)\n"
        f"🎯 Target 2: ₹{target:,.0f} (+5%)\n"
        f"🛑 Stop Loss: ₹{stoploss:,.0f} (-3%)"
    )


def _generate_exit_hint(symbol: str) -> str:
    now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    market_hours = 9 <= now.hour < 15 or (now.hour == 15 and now.minute <= 30)
    timing = "EXIT NOW" if market_hours else "EXIT AT OPEN TOMORROW"

    return (
        f"🕐 Timing: {timing}\n"
        f"⚠️ Avoid new positions. Consider reducing existing holdings.\n"
        f"📉 Expected downside: -3% to -7% in near term"
    )


def deal_type_label(deal: Dict) -> str:
    dt = deal.get("deal_type", "deal").upper()
    snippet = deal.get("snippet", "")[:60]
    return f"{dt} DEAL: {snippet}"


def _net_deal_label(nd: Dict) -> str:
    """Human-readable label for an aggregated net-deal signal."""
    side = "Net BUY" if nd.get("direction") == "BULLISH" else "Net SELL"
    return f"Bulk/Block {side} ({nd.get('buy', 0)} buy / {nd.get('sell', 0)} sell)"


# ─────────────────────────────────────────────────────────────────────
#  Master Predictor — runs all checks and returns consolidated alerts
# ─────────────────────────────────────────────────────────────────────

def run_predictor(
    oi_alerts: List[Dict],
    deals: Dict[str, List[Dict]],
    filing_alerts: List[Dict],
    index_signals: List[Dict],
    oi_data: Dict[str, Dict],
    banned_stocks: List[str],
    delivery_alerts: List[Dict] = None,
    universe: set = None,
) -> Dict[str, List[Dict]]:
    """Run all predictive checks and return categorized results.

    ``universe`` restricts alerts to the tracked (F&O/Nifty) symbols so that
    illiquid small-caps surfaced by bulk deals never generate signals.
    """
    global TRACKED_UNIVERSE
    if universe:
        TRACKED_UNIVERSE = set(universe)

    _oi_concentration["last"] = check_oi_concentration(oi_data)
    # F&O ban only applies to tracked symbols; keep the concentration path clean.
    _oi_concentration["banned"] = {s for s in banned_stocks if _in_universe(s)}

    # Augment OI alerts with delivery alerts (since NSE option chain is blocked)
    if delivery_alerts:
        oi_alerts = list(oi_alerts) + list(delivery_alerts)

    entries = check_predictive_entry(oi_alerts, deals, filing_alerts, index_signals, oi_data)
    exits = check_predictive_exit(oi_alerts, deals, filing_alerts, index_signals, oi_data)
    sure = check_sure_shot(entries, exits)
    rotation = check_sector_rotation(entries, exits)

    return {
        "entries": entries,
        "exits": exits,
        "sure_shots": sure,
        "sector_rotation": rotation,
        "concentration": _oi_concentration["last"],
    }
