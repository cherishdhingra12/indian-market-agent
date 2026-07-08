"""
Signal detection rules for Indian stock market.
Pure logic — no I/O. Takes data from deep_sources and returns alert decisions.

Rule types:
  - OI + Price convergence (long/short buildup)
  - Delivery % spikes (accumulation/distribution)
  - Multi-signal convergence (bulk deal + delivery + OI)
  - Index-level VIX / FII signals
  - Filing impact classification
"""

import logging
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)

# Avoid circular import: deep_sources's FNO_STOCKS is used in check_fno_ban
FNO_STOCKS = set()  # populated at runtime by signal_monitor

# ─────────────────────────────────────────────────────────────────────
#  Constants
# ─────────────────────────────────────────────────────────────────────

OI_CHANGE_THRESHOLD = 15.0       # % OI change to trigger alert
OI_CHANGE_HIGH = 30.0             # % OI change for high conviction
PRICE_CHANGE_THRESHOLD = 2.0      # % price change with OI
DELIVERY_SPIKE_THRESHOLD = 85.0   # delivery % for accumulation signal
DELIVERY_DROP_THRESHOLD = 30.0    # delivery % for distribution signal
BULK_DEAL_CRORE_THRESHOLD = 100   # ₹ Cr for bulk deal alert
VIX_SPIKE_THRESHOLD = 20.0        # % VIX change in short period
FII_SELL_THRESHOLD = 1500         # ₹ Cr FII net sell
DII_BUY_THRESHOLD = 1500          # ₹ Cr DII net buy
BAN_LIST_ALERT = True

HIGH_IMPACT_FILING_CATS = {
    "Outcome of Board Meeting",
    "Awarding of order(s)/contract(s)",
    "Bagging/Receiving of orders/contracts",
    "Fund Raising",
    "Allotment of Securities",
    "Allotment of ESOP / ESPS",
    "Preferential Issue",
    "Buy Back",
    "Bonus",
    "Stock Split",
    "Dividend Update",
    "Dividend",
    "Appointment",
    "Resignation",
    "Cessation",
    "Analysts/Institutional Investor Meet/Con. Call Updates",
    "Updates",
    "General Updates",
    "Press Release",
    "Credit Rating",
}


# ─────────────────────────────────────────────────────────────────────
#  Rule 1: OI + Price Convergence
# ─────────────────────────────────────────────────────────────────────

def check_oi_convergence(
    current_oi: Dict[str, Dict],
    previous_oi: Optional[Dict[str, Dict]] = None,
) -> List[Dict]:
    """
    Detect OI buildup/destruction:
      - Long Buildup: OI up + Price up
      - Short Buildup: OI up + Price down
      - Long Unwinding: OI down + Price down
      - Short Covering: OI down + Price up
    """
    alerts = []
    if not previous_oi:
        return alerts

    for symbol, curr in current_oi.items():
        prev = previous_oi.get(symbol)
        if not prev:
            continue

        curr_total_oi = curr.get("total_oi", 0) or 0
        prev_total_oi = prev.get("total_oi", 0) or 0
        curr_price = curr.get("underlying_value", 0) or 0
        prev_price = prev.get("underlying_value", 0) or 0

        if prev_total_oi == 0 or prev_price == 0:
            continue

        oi_change = round((curr_total_oi - prev_total_oi) / prev_total_oi * 100, 2)
        price_change = round((curr_price - prev_price) / prev_price * 100, 2)

        if abs(oi_change) < OI_CHANGE_THRESHOLD:
            continue

        if oi_change > OI_CHANGE_THRESHOLD and price_change > PRICE_CHANGE_THRESHOLD:
            alerts.append({
                "symbol": symbol,
                "signal": "LONG_BUILDUP",
                "confidence": "HIGH" if oi_change > OI_CHANGE_HIGH else "MEDIUM",
                "oi_change_pct": oi_change,
                "price_change_pct": price_change,
                "oi_direction": "up",
                "price_direction": "up",
                "reason": f"OI +{oi_change}%, Price +{price_change}% — Longs adding positions",
            })
        elif oi_change > OI_CHANGE_THRESHOLD and price_change < -PRICE_CHANGE_THRESHOLD:
            alerts.append({
                "symbol": symbol,
                "signal": "SHORT_BUILDUP",
                "confidence": "HIGH" if oi_change > OI_CHANGE_HIGH else "MEDIUM",
                "oi_change_pct": oi_change,
                "price_change_pct": price_change,
                "oi_direction": "up",
                "price_direction": "down",
                "reason": f"OI +{oi_change}%, Price {price_change}% — Shorts building positions",
            })
        elif oi_change < -OI_CHANGE_THRESHOLD and price_change < -PRICE_CHANGE_THRESHOLD:
            alerts.append({
                "symbol": symbol,
                "signal": "LONG_UNWINDING",
                "confidence": "MEDIUM",
                "oi_change_pct": oi_change,
                "price_change_pct": price_change,
                "oi_direction": "down",
                "price_direction": "down",
                "reason": f"OI {oi_change}%, Price {price_change}% — Longs exiting positions",
            })
        elif oi_change < -OI_CHANGE_THRESHOLD and price_change > PRICE_CHANGE_THRESHOLD:
            alerts.append({
                "symbol": symbol,
                "signal": "SHORT_COVERING",
                "confidence": "MEDIUM",
                "oi_change_pct": oi_change,
                "price_change_pct": price_change,
                "oi_direction": "down",
                "price_direction": "up",
                "reason": f"OI {oi_change}%, Price +{price_change}% — Shorts covering positions",
            })

    log.info(f"OI convergence: {len(alerts)} signals detected")
    return alerts


# ─────────────────────────────────────────────────────────────────────
#  Rule 2: Delivery % Spikes
# ─────────────────────────────────────────────────────────────────────

def check_delivery_spikes(delivery_snapshots: Dict[str, Dict]) -> List[Dict]:
    """
    Detect accumulation / distribution from a single EOD bhavcopy:
      - High delivery % (>=85%) + price up   = accumulation (bullish)
      - Low delivery %  (<=30%) + price down = distribution (bearish)

    Note: a single bhavcopy has no previous-day delivery %, so a genuine
    day-over-day "delivery surge" cannot be computed from it. Direction and
    magnitude use ``p_change`` (the real percent price move); the raw ``change``
    field is rupees, not a percentage, and must not be printed as "%".
    """
    alerts = []
    for symbol, data in delivery_snapshots.items():
        delivery_pct = data.get("delivery_pct", 0) or 0
        p_change = data.get("p_change", 0) or 0
        total_qty = data.get("total_qty", 0) or 0

        if total_qty < 100000:
            continue

        # Bhavcopy is previous-session EOD data — label it so a signal sent
        # intraday isn't mistaken for a live tick.
        raw_date = data.get("date", "")
        if raw_date and len(raw_date) == 8:
            src = f" [EOD bhavcopy {raw_date[:2]}-{raw_date[2:4]}-{raw_date[4:]}]"
        else:
            src = " [EOD bhavcopy]"

        close_price = data.get("last_price", 0) or 0
        if delivery_pct >= DELIVERY_SPIKE_THRESHOLD and p_change > 0:
            alerts.append({
                "symbol": symbol,
                "signal": "ACCUMULATION",
                "confidence": "HIGH" if delivery_pct >= 90 else "MEDIUM",
                "delivery_pct": delivery_pct,
                "change": round(p_change, 2),
                "last_price": close_price,
                "reason": f"Delivery {delivery_pct}% + price {p_change:+.2f}% — Strong hands accumulating{src}",
            })
        elif delivery_pct <= DELIVERY_DROP_THRESHOLD and p_change < 0:
            alerts.append({
                "symbol": symbol,
                "signal": "DISTRIBUTION",
                "confidence": "MEDIUM",
                "delivery_pct": delivery_pct,
                "change": round(p_change, 2),
                "last_price": close_price,
                "reason": f"Delivery {delivery_pct}% + price {p_change:+.2f}% — Weak hands / distribution{src}",
            })

    log.info(f"Delivery spikes: {len(alerts)} signals detected")
    return alerts


# ─────────────────────────────────────────────────────────────────────
#  Rule 3: Multi-Signal Convergence
# ─────────────────────────────────────────────────────────────────────

def check_multi_convergence(
    oi_alerts: List[Dict],
    delivery_alerts: List[Dict],
    bulk_deals: List[Dict],
    block_deals: List[Dict],
) -> List[Dict]:
    """
    Combine multiple signal types for the same stock.
    When 2+ signal types fire on same stock = high conviction alert.
    """
    signal_map = {}
    for alert in oi_alerts:
        sym = alert["symbol"]
        signal_map.setdefault(sym, []).append(("OI", alert))

    for alert in delivery_alerts:
        sym = alert["symbol"]
        signal_map.setdefault(sym, []).append(("DELIVERY", alert))

    # Check bulk/block deals for same stock
    for deal in bulk_deals + block_deals:
        sym = deal.get("symbol", "")
        if sym:
            signal_map.setdefault(sym, []).append(("DEAL", deal))

    convergences = []
    for symbol, signals in signal_map.items():
        if len(signals) >= 2:
            types = [s[0] for s in signals]
            reasons = []
            for stype, sdata in signals:
                if stype == "OI":
                    reasons.append(f"OI {sdata['oi_change_pct']}% / {sdata['price_change_pct']}%")
                elif stype == "DELIVERY":
                    reasons.append(f"Delivery {sdata['delivery_pct']}%")
                elif stype == "DEAL":
                    reasons.append(f"{sdata.get('deal_type', 'deal').upper()} ₹{sdata.get('snippet', '')[:50]}")

            convergences.append({
                "symbol": symbol,
                "signal": "CONVERGENCE",
                "confidence": "HIGH" if len(signals) >= 3 else "MEDIUM",
                "signal_count": len(signals),
                "signal_types": types,
                "reasons": reasons,
                "reason": f"Multi-signal convergence: {' | '.join(reasons)}",
            })

    log.info(f"Multi-signal convergence: {len(convergences)} detected")
    return convergences


# ─────────────────────────────────────────────────────────────────────
#  Rule 4: Index-Level Signals
# ─────────────────────────────────────────────────────────────────────

def check_index_signals(
    vix_data: Dict = None,
    fii_data: Dict = None,
    gift_nifty: Dict = None,
    usd_inr: Dict = None,
) -> List[Dict]:
    """Detect market-wide index-level signals."""
    alerts = []

    if vix_data:
        vix = vix_data.get("vix", 0)
        change = vix_data.get("change_pct", 0)
        if change > VIX_SPIKE_THRESHOLD and vix > 20:
            alerts.append({
                "symbol": "INDIA VIX",
                "signal": "VIX_SPIKE",
                "confidence": "HIGH",
                "value": vix,
                "change_pct": change,
                "reason": f"VIX spiked {change}% to {vix} — Market fear rising, expect volatility",
            })
        elif change < -VIX_SPIKE_THRESHOLD and vix < 15:
            alerts.append({
                "symbol": "INDIA VIX",
                "signal": "VIX_DROP",
                "confidence": "MEDIUM",
                "value": vix,
                "change_pct": change,
                "reason": f"VIX dropped {change}% to {vix} — Market complacency, rally may continue",
            })

    if fii_data:
        fii_sell = abs(fii_data.get("fii_cash_cr", 0))
        dii_buy = fii_data.get("dii_cash_cr", 0)
        if fii_sell > FII_SELL_THRESHOLD:
            alerts.append({
                "symbol": "FII FLOW",
                "signal": "FII_SELLING",
                "confidence": "HIGH" if fii_sell > 3000 else "MEDIUM",
                "value": -fii_sell,
                "reason": f"FIIs net sold ₹{fii_sell:,.0f}Cr in cash — Institutional selling pressure",
            })
        if dii_buy > DII_BUY_THRESHOLD:
            alerts.append({
                "symbol": "DII FLOW",
                "signal": "DII_BUYING",
                "confidence": "MEDIUM",
                "value": dii_buy,
                "reason": f"DIIs net bought ₹{dii_buy:,.0f}Cr — Domestic institutions stepping in",
            })

    if gift_nifty:
        change = gift_nifty.get("change_pct", 0)
        if change < -1.0:
            alerts.append({
                "symbol": "GIFT NIFTY",
                "signal": "PRE_MARKET_DOWN",
                "confidence": "HIGH" if change < -1.5 else "MEDIUM",
                "value": change,
                "reason": f"GIFT Nifty {change}% — Market likely to open negative",
            })
        elif change > 1.0:
            alerts.append({
                "symbol": "GIFT NIFTY",
                "signal": "PRE_MARKET_UP",
                "confidence": "MEDIUM",
                "value": change,
                "reason": f"GIFT Nifty +{change}% — Market likely to open positive",
            })

    if usd_inr:
        rate = usd_inr.get("rate", 0)
        if rate > 84:
            alerts.append({
                "symbol": "USD/INR",
                "signal": "RUPEE_WEAK",
                "confidence": "MEDIUM",
                "value": rate,
                "reason": f"USD/INR at ₹{rate} — Rupee weakening, IT stocks may benefit, importers under pressure",
            })

    log.info(f"Index signals: {len(alerts)} detected")
    return alerts


# ─────────────────────────────────────────────────────────────────────
#  Rule 5: Filing Impact Classification
# ─────────────────────────────────────────────────────────────────────

def classify_filing_impact(filing: Dict, stock_universe: set) -> Optional[Dict]:
    """
    Classify an NSE corporate filing by impact level.
    High impact = board outcome for Nifty 50 stock, order win, etc.
    """
    symbol = filing.get("symbol", "").upper()
    category = (filing.get("category") or "").strip()
    company = (filing.get("company") or "").strip()
    text = (filing.get("snippet") or "").lower()

    if not symbol or symbol not in stock_universe:
        return None

    if category not in HIGH_IMPACT_FILING_CATS:
        return None

    direction = "neutral"
    confidence = "MEDIUM"

    # Board outcome: check for dividend, buyback, etc.
    if "Outcome of Board Meeting" in category:
        confidence = "HIGH"
        if any(kw in text for kw in ["buyback", "buy back", "bonus", "stock split"]):
            direction = "bullish"
        elif any(kw in text for kw in ["dividend", "interim dividend", "final dividend"]):
            direction = "bullish"
        elif any(kw in text for kw in ["resignation", "cessation", "loss", "decline"]):
            direction = "bearish"

    # Order wins
    elif "order" in category.lower() or "contract" in category.lower():
        direction = "bullish"
        confidence = "HIGH"

    # Fund raising
    elif "Fund Raising" in category or "Preferential Issue" in category:
        direction = "neutral"
        if "qip" in text or "qualified institutions" in text:
            direction = "neutral"  # could be dilutive
        elif "rights" in text:
            direction = "neutral"  # rights issue

    # Appointment / resignation
    elif "Appointment" in category:
        direction = "neutral"
        if any(kw in text for kw in ["cfo", "ceo", "managing director", "whole-time director"]):
            direction = "bullish"  # key appointment = confidence
            confidence = "HIGH"
    elif "Resignation" in category or "Cessation" in category:
        direction = "bearish"
        if any(kw in text for kw in ["cfo", "ceo", "managing director"]):
            confidence = "HIGH"
            direction = "bearish"

    # Credit rating
    elif "Credit Rating" in category:
        if any(kw in text for kw in ["upgrade", "revised upwards", "enhanced"]):
            direction = "bullish"
            confidence = "HIGH"
        elif any(kw in text for kw in ["downgrade", "revised downwards"]):
            direction = "bearish"
            confidence = "HIGH"

    # Analyst meet
    elif "Analysts" in category or "Institutional Investor" in category:
        direction = "neutral"
        confidence = "LOW"

    return {
        "symbol": symbol,
        "signal": f"FILING_{direction.upper()}" if direction != "neutral" else "FILING_NEUTRAL",
        "confidence": confidence,
        "direction": direction,
        "category": category,
        "company": company,
        "reason": f"{symbol}: {category} — {text[:200]}",
    }


# ─────────────────────────────────────────────────────────────────────
#  Rule 6: F&O Ban Alert
# ─────────────────────────────────────────────────────────────────────

def check_fno_ban(banned_stocks: List[str], previous_banned: List[str] = None) -> List[Dict]:
    """Detect new stock added to F&O ban."""
    alerts = []
    if not previous_banned:
        return alerts

    new_bans = set(banned_stocks) - set(previous_banned)
    for symbol in new_bans:
        if symbol in set(FNO_STOCKS):
            alerts.append({
                "symbol": symbol,
                "signal": "FNO_BAN",
                "confidence": "HIGH",
                "reason": f"{symbol} added to F&O ban — MWPL > 95%, avoid new long positions",
            })

    return alerts
