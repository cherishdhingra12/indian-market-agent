"""
Deep / non-obvious data sources for Indian stock market signals.
All sources are public — NSE exchange data, free APIs, and web scraping.

Sources:
  1. F&O Option Chain OI      — NSE API (per-stock, total OI) + yfinance fallback for prices
  2. Delivery %                — NSE quote-equity security info
  3. FII/DII Net Flows         — NSE FII/DII stats API  
  4. India VIX                 — NSE chart data index
  5. GIFT Nifty (pre-market)   — Google Finance scrape
  6. USD/INR Currency          — Free exchangerate API
  7. Bulk/Block Deals          — Reused from insider_scrapers.py
  8. F&O Ban List              — NSE securities ban list (HTML scrape)
"""

import json
import logging
import re
import time
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional
from urllib.parse import urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────
#  Stock Universe
# ─────────────────────────────────────────────────────────────────────

NIFTY_50 = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "HINDUNILVR", "ICICIBANK",
    "ITC", "SBIN", "BHARTIARTL", "KOTAKBANK", "LT", "WIPRO",
    "AXISBANK", "BAJFINANCE", "ASIANPAINT", "MARUTI", "SUNPHARMA",
    "TATAMOTORS", "NTPC", "M&M", "TITAN", "JSWSTEEL", "POWERGRID",
    "NESTLEIND", "TECHM", "HCLTECH", "BAJAJFINSV", "ULTRACEMCO",
    "SBILIFE", "TATASTEEL", "GRASIM", "CIPLA", "DABUR", "HINDALCO",
    "ONGC", "ADANIPORTS", "DRREDDY", "EICHERMOT", "INDUSINDBK",
    "BAJAJAUTO", "DIVISLAB", "BRITANNIA", "COALINDIA", "BPCL",
    "SHRIRAMFIN", "ADANIENT", "HEROMOTOCO", "HDFCLIFE", "APOLLOHOSP",
    "ADANIGREEN", "ADANITRANS", "ICICIPRULI", "SBICARD", "DMART",
    "TATACONSUM", "HAVELLS", "TORPHARMA", "MARICO", "BERGEPAINT",
    "BANDHANBNK", "PIDILITIND", "TRENT",
]

BANK_NIFTY = [
    "HDFCBANK", "ICICIBANK", "KOTAKBANK", "SBIN", "AXISBANK",
    "INDUSINDBK", "BANKBARODA", "PNB", "FEDERALBNK", "IDFCFIRSTB",
    "YESBANK", "BANDHANBNK", "AUBANK",
]

FNO_STOCKS = sorted(set(NIFTY_50 + BANK_NIFTY))

# ─────────────────────────────────────────────────────────────────────
#  NSE Session
# ─────────────────────────────────────────────────────────────────────

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]

_ua_index = 0


def _session() -> requests.Session:
    global _ua_index
    session = requests.Session()
    ua = USER_AGENTS[_ua_index % len(USER_AGENTS)]
    _ua_index += 1
    session.headers.update({
        "User-Agent": ua,
        "Accept-Language": "en-US,en;q=0.9,hi;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "DNT": "1",
    })
    time.sleep(0.3)
    return session


def _nse_session() -> Optional[requests.Session]:
    try:
        session = _session()
        session.get("https://www.nseindia.com", timeout=10)
        time.sleep(0.6)
        return session
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────
#  1. F&O Option Chain — OI Data
# ─────────────────────────────────────────────────────────────────────

def scrape_option_chain_oi(symbol: str) -> Optional[Dict]:
    """Fetch option chain for a stock and calculate aggregate OI metrics."""
    session = _nse_session()
    if not session:
        return None
    try:
        resp = session.get(
            f"https://www.nseindia.com/api/option-chain-equities?symbol={symbol}",
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        records = data.get("records", {})
        strike_data = records.get("data", [])
        if not strike_data:
            return None

        total_ce_oi = 0
        total_pe_oi = 0
        total_ce_oi_change = 0
        total_pe_oi_change = 0
        for item in strike_data:
            ce = item.get("CE") or {}
            pe = item.get("PE") or {}
            total_ce_oi += ce.get("openInterest", 0) or 0
            total_pe_oi += pe.get("openInterest", 0) or 0
            total_ce_oi_change += ce.get("changeinOpenInterest", 0) or 0
            total_pe_oi_change += pe.get("changeinOpenInterest", 0) or 0

        return {
            "symbol": symbol,
            "underlying_value": records.get("underlyingValue"),
            "total_ce_oi": total_ce_oi,
            "total_pe_oi": total_pe_oi,
            "ce_oi_change": total_ce_oi_change,
            "pe_oi_change": total_pe_oi_change,
            "total_oi": total_ce_oi + total_pe_oi,
            "total_oi_change": total_ce_oi_change + total_pe_oi_change,
            "put_call_ratio": round(total_pe_oi / max(total_ce_oi, 1), 4),
        }
    except Exception as e:
        log.debug(f"Option chain OI failed for {symbol}: {e}")
        return None
    finally:
        session.close()


def get_fno_oi_snapshots(symbols: List[str] = None) -> Dict[str, Dict]:
    if symbols is None:
        symbols = FNO_STOCKS
    results = {}
    for i, symbol in enumerate(symbols):
        data = scrape_option_chain_oi(symbol)
        if data:
            results[symbol] = data
        if i > 0 and i % 10 == 0:
            time.sleep(1)
        else:
            time.sleep(0.4)
    log.info(f"F&O OI snapshots: {len(results)}/{len(symbols)}")
    return results


# ─────────────────────────────────────────────────────────────────────
#  2. Delivery % (via NSE quote-equity API)
# ─────────────────────────────────────────────────────────────────────

_quote_cache: Dict[str, tuple] = {}

def scrape_quote_equity(symbol: str) -> Optional[Dict]:
    """Fetch basic price/delivery data from NSE quote-equity."""
    session = _nse_session()
    if not session:
        return None
    try:
        resp = session.get(
            f"https://www.nseindia.com/api/quote-equity?symbol={symbol}",
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        price_info = data.get("priceInfo", {}) or {}
        return {
            "symbol": symbol,
            "last_price": price_info.get("lastPrice"),
            "change": price_info.get("change"),
            "p_change": price_info.get("pChange"),
            "total_volume": (data.get("data") or [{}])[0].get("totalTradedVolume"),
            "delivery_pct": (data.get("securityInfo") or {}).get("deliveryToTradedQuantity"),
        }
    except Exception:
        return None
    finally:
        session.close()


def get_delivery_snapshots(symbols: List[str] = None) -> Dict[str, Dict]:
    """NOT AVAILABLE — quote-equity API is blocked by NSE (returns 403).
    This function is kept as a no-op placeholder for future use."""
    return {}


# ─────────────────────────────────────────────────────────────────────
#  3. FII / DII Net Flows
# ─────────────────────────────────────────────────────────────────────

def scrape_fii_dii_flows() -> Optional[Dict]:
    """NOT AVAILABLE — NSE fiidiistats endpoint removed.
    This function is kept as a no-op placeholder for future use."""
    return None


# ─────────────────────────────────────────────────────────────────────
#  4. India VIX
# ─────────────────────────────────────────────────────────────────────

def scrape_india_vix() -> Optional[Dict]:
    """Fetch India VIX current value from NSE."""
    session = _nse_session()
    if not session:
        return None
    try:
        resp = session.get(
            "https://www.nseindia.com/api/chart-databyindex?index=INDIAVIX",
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        series = data.get("grapthData", [])
        if series and len(series) > 1:
            latest = series[-1]
            current = latest[1] if len(latest) > 1 else None
            prev_close = data.get("closePrice")
            if current and prev_close and prev_close > 0:
                change_pct = round((current - prev_close) / prev_close * 100, 2)
                return {
                    "vix": current,
                    "change_pct": change_pct,
                    "direction": "up" if change_pct > 0 else "down",
                }
        return None
    except Exception as e:
        log.debug(f"India VIX fetch failed: {e}")
        return None
    finally:
        session.close()


# ─────────────────────────────────────────────────────────────────────
#  5. GIFT Nifty — Pre-Market Indication
# ─────────────────────────────────────────────────────────────────────

def scrape_gift_nifty() -> Optional[Dict]:
    """Scrape GIFT Nifty / Nifty Futures from Google Finance."""
    try:
        resp = requests.get(
            "https://www.google.com/finance/quote/NIFTY:INDEXNSE",
            headers={"User-Agent": USER_AGENTS[0]},
            timeout=10,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        price_el = soup.select_one("[data-last-price]")
        if not price_el:
            return None
        price = float(price_el.get("data-last-price", "0"))
        change_div = soup.select_one(".JwB6zf")
        change_text = change_div.get_text(strip=True) if change_div else ""
        match = re.search(r"([+-]?\d+\.?\d*)", change_text.replace(",", ""))
        change_val = float(match.group(1)) if match else 0
        prev = price - change_val
        pct = round(change_val / max(abs(prev), 1) * 100, 2) if prev != 0 else 0
        return {
            "price": price,
            "change": round(change_val, 2),
            "change_pct": pct,
            "direction": "up" if change_val > 0 else "down",
        }
    except Exception as e:
        log.debug(f"GIFT Nifty scrape failed: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────
#  6. USD/INR Currency Rate
# ─────────────────────────────────────────────────────────────────────

def scrape_usd_inr() -> Optional[Dict]:
    try:
        resp = requests.get(
            "https://api.exchangerate-api.com/v4/latest/USD", timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
        inr = (data.get("rates") or {}).get("INR")
        if inr:
            return {"rate": inr, "currency": "USD/INR", "date": data.get("date", "")}
        return None
    except Exception as e:
        log.warning(f"USD/INR failed: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────
#  7. NSE Bulk / Block Deals
# ─────────────────────────────────────────────────────────────────────

def get_bulk_block_deals() -> Dict[str, List[Dict]]:
    try:
        from insider_scrapers import _scrape_nse_large_deals
        deals = _scrape_nse_large_deals()
        return {"bulk": deals.get("bulk", []), "block": deals.get("block", [])}
    except Exception as e:
        log.warning(f"Bulk/block deals failed: {e}")
        return {"bulk": [], "block": []}


# ─────────────────────────────────────────────────────────────────────
#  8. F&O Ban List
# ─────────────────────────────────────────────────────────────────────

def scrape_fno_ban_list() -> List[str]:
    try:
        resp = requests.get(
            "https://www.nseindia.com/regulations/market/fo-securities-ban",
            headers={"User-Agent": USER_AGENTS[0]},
            timeout=10,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        banned = []
        for row in soup.select("table tr"):
            cells = row.select("td")
            if cells:
                el = cells[0].select_one("a")
                if el:
                    sym = el.get_text(strip=True).upper()
                    if sym and len(sym) <= 15 and sym.isalpha():
                        banned.append(sym)
        log.info(f"F&O Ban list: {len(banned)} stocks")
        return banned
    except Exception as e:
        log.debug(f"F&O ban list failed: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────
#  Utility: Market Status
# ─────────────────────────────────────────────────────────────────────

def ist_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)


def is_market_open() -> bool:
    now = ist_now()
    if now.weekday() >= 5:
        return False
    return now.replace(hour=9, minute=15) <= now <= now.replace(hour=15, minute=30)


def is_premarket() -> bool:
    now = ist_now()
    if now.weekday() >= 5:
        return False
    return now.replace(hour=8, minute=0) <= now < now.replace(hour=9, minute=15)


def is_postmarket() -> bool:
    now = ist_now()
    if now.weekday() >= 5:
        return False
    return now.replace(hour=15, minute=30) <= now <= now.replace(hour=20, minute=0)
