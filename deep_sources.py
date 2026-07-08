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


def _nse_curl_session():
    """
    NSE-primed session using curl_cffi (real Chrome TLS fingerprint).

    NSE's Akamai edge now 403s the default python-requests TLS handshake at the
    homepage, which silently breaks every downstream API call. curl_cffi
    impersonates Chrome and gets a 200 + valid cookies. Returns None if
    curl_cffi is unavailable so callers can degrade gracefully.
    """
    try:
        from curl_cffi import requests as curl_req
    except Exception:
        log.debug("curl_cffi unavailable — NSE API calls may be blocked")
        return None
    try:
        s = curl_req.Session(impersonate="chrome124")
        s.headers.update({
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.nseindia.com/",
        })
        s.get("https://www.nseindia.com", timeout=15)
        time.sleep(0.5)
        return s
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────
#  1. F&O Option Chain — OI Data
# ─────────────────────────────────────────────────────────────────────

def scrape_option_chain_oi(symbol: str, session=None) -> Optional[Dict]:
    """Fetch option chain for a stock and calculate aggregate OI metrics.

    Note: NSE currently gates the option-chain-equities endpoint (returns an
    empty payload even with a valid Chrome TLS session). When that happens we
    return None so the caller stays silent rather than emitting fake OI signals.
    Pass a shared curl_cffi ``session`` to avoid re-priming per symbol.
    """
    own_session = session is None
    if own_session:
        session = _nse_curl_session()
    if session is None:
        return None
    try:
        resp = session.get(
            f"https://www.nseindia.com/api/option-chain-equities?symbol={symbol}",
            timeout=15,
        )
        if resp.status_code != 200:
            return None
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
        if own_session:
            try:
                session.close()
            except Exception:
                pass


def get_fno_oi_snapshots(symbols: List[str] = None) -> Dict[str, Dict]:
    if symbols is None:
        symbols = FNO_STOCKS
    session = _nse_curl_session()
    if session is None:
        log.warning("F&O OI: no NSE session (curl_cffi unavailable)")
        return {}
    results = {}
    try:
        for i, symbol in enumerate(symbols):
            data = scrape_option_chain_oi(symbol, session=session)
            if data:
                results[symbol] = data
            if i > 0 and i % 10 == 0:
                time.sleep(1)
            else:
                time.sleep(0.4)
    finally:
        try:
            session.close()
        except Exception:
            pass
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
    """Fetch delivery % from NSE equity bhavcopy (working data source).
    Uses curl_cffi for proper TLS fingerprint to bypass Akamai."""
    if symbols is None:
        symbols = FNO_STOCKS
    try:
        from curl_cffi import requests as curl_req
        import csv, io

        # Try yesterday first (today's bhavcopy not published until EOD)
        for days_back in [1, 2, 0]:
            d = (datetime.now() - timedelta(days=days_back)).date()
            date_str = d.strftime('%d%m%Y')
            url = f'https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{date_str}.csv'
            s = curl_req.Session(impersonate='chrome124')
            resp = s.get(url, timeout=20)
            if resp.status_code != 200 or len(resp.content) < 10000:
                continue

            content = resp.text
            reader = csv.DictReader(io.StringIO(content), skipinitialspace=True)
            symbol_set = set(s.upper() for s in symbols)
            results = {}
            for row in reader:
                sym = row.get('SYMBOL', '').upper()
                if sym not in symbol_set:
                    continue
                series = row.get('SERIES', '').strip()
                if series not in ('EQ', 'BE', 'BZ'):
                    continue
                try:
                    last_price = float(row.get('CLOSE_PRICE', 0) or 0)
                    prev_close = float(row.get('PREV_CLOSE', 0) or 0)
                    change = last_price - prev_close
                    p_change = round(change / prev_close * 100, 2) if prev_close else 0
                    total_qty = int(row.get('TTL_TRD_QNTY', 0) or 0)
                    deliv_qty = int(row.get('DELIV_QTY', 0) or 0)
                    delivery_pct = round(deliv_qty / total_qty * 100, 2) if total_qty else 0

                    results[sym] = {
                        "symbol": sym,
                        "last_price": last_price,
                        "change": round(change, 2),
                        "p_change": p_change,
                        "total_volume": total_qty,
                        "delivery_pct": delivery_pct,
                        "deliv_qty": deliv_qty,
                        "total_qty": total_qty,
                        "date": date_str,
                    }
                except (ValueError, TypeError):
                    continue

            if results:
                log.info(f"Delivery snapshots: {len(results)} stocks from bhavcopy {date_str}")
                return results

        log.warning("No bhavcopy available for last 3 days")
        return {}
    except Exception as e:
        log.warning(f"Bhavcopy delivery fetch failed: {e}")
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
    """Fetch India VIX from NSE's allIndices endpoint.

    The old chart-databyindex?index=INDIAVIX endpoint now returns empty data.
    India VIX is still published in /api/allIndices as a regular index row,
    which includes the live value and day change.
    """
    session = _nse_curl_session()
    if session is None:
        return None
    try:
        resp = session.get("https://www.nseindia.com/api/allIndices", timeout=20)
        if resp.status_code != 200:
            return None
        data = resp.json()
        for idx in data.get("data", []):
            if "VIX" in (idx.get("index") or "").upper():
                current = idx.get("last")
                change_pct = idx.get("percentChange")
                if current is None or change_pct is None:
                    return None
                return {
                    "vix": round(float(current), 2),
                    "change_pct": round(float(change_pct), 2),
                    "direction": "up" if float(change_pct) > 0 else "down",
                }
        return None
    except Exception as e:
        log.debug(f"India VIX fetch failed: {e}")
        return None
    finally:
        try:
            session.close()
        except Exception:
            pass


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
    session = _nse_curl_session()
    if session is None:
        return []
    try:
        resp = session.get(
            "https://www.nseindia.com/api/reportOI-SecuritiesBan",
            timeout=15,
        )
        banned = []
        if resp.status_code == 200:
            data = resp.json()
            # API shape: {"data": ["SYMBOL1", "SYMBOL2", ...]} or list of dicts
            rows = data.get("data", data) if isinstance(data, dict) else data
            for row in rows or []:
                sym = row if isinstance(row, str) else (row.get("symbol") or row.get("Symbol") or "")
                sym = sym.strip().upper()
                if sym and len(sym) <= 15 and sym.isalpha():
                    banned.append(sym)
        # Fallback: HTML regulations page
        if not banned:
            r2 = session.get(
                "https://www.nseindia.com/regulations/market/fo-securities-ban",
                timeout=15,
            )
            soup = BeautifulSoup(r2.text, "lxml")
            for row in soup.select("table tr"):
                cells = row.select("td")
                if cells:
                    el = cells[0].select_one("a") or cells[0]
                    sym = el.get_text(strip=True).upper()
                    if sym and len(sym) <= 15 and sym.isalpha():
                        banned.append(sym)
        log.info(f"F&O Ban list: {len(banned)} stocks")
        return banned
    except Exception as e:
        log.debug(f"F&O ban list failed: {e}")
        return []
    finally:
        try:
            session.close()
        except Exception:
            pass


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
