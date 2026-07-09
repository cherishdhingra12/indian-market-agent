"""Zerodha Kite Connect — LIVE options/futures OI + price for real-time signals.

Restores the bot's core real-time signal: F&O Open Interest buildup. NSE's free
option-chain endpoint is dead, so we use the Kite Connect API (paid tier, live
market data). Requires a Zerodha account + API key/secret, and a daily
access_token (Kite tokens expire ~6am next day — regenerate each morning).

Two data products, both from ONE batched quote() call per poll:
  1. STOCK OI  — each F&O stock's current-month FUTURES contract carries live
     total OI + last price. Long/short-buildup analysis is textbook on futures OI
     (OI up + price up = longs adding; OI up + price down = shorts building).
  2. INDEX OI  — NIFTY/BANKNIFTY current-month futures for the same buildup logic.

Output matches the existing scraper's shape so the rest of the pipeline is
unchanged: {symbol: {"symbol","total_oi","underlying_value", ...}}.

Auth (env, loaded from .env):
  KITE_API_KEY, KITE_API_SECRET, KITE_ACCESS_TOKEN   (access_token = daily)
Helpers: `login_url()` prints the login link; `exchange_token(request_token)`
swaps the one-time request_token for a daily access_token.
"""
import os
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

# Index futures we also want buildup signals on (Kite tradingsymbols use these roots)
INDEX_ROOTS = ["NIFTY", "BANKNIFTY"]

_kite = None                 # cached KiteConnect client
_fut_map: Dict[str, str] = {}   # symbol -> current-month FUT tradingsymbol
_token_map: Dict[str, int] = {}  # "NFO:TRADINGSYMBOL" -> instrument_token (unused for quote())


def _creds() -> Optional[dict]:
    k = os.environ.get("KITE_API_KEY", "")
    t = os.environ.get("KITE_ACCESS_TOKEN", "")
    if not k or not t or "YOUR_" in k or "YOUR_" in t:
        return None
    return {"api_key": k, "access_token": t}


def available() -> bool:
    """True only if Kite creds are present — lets callers fall back cleanly."""
    return _creds() is not None


def _client():
    """Return an authenticated KiteConnect client, or None if unavailable."""
    global _kite
    if _kite is not None:
        return _kite
    c = _creds()
    if not c:
        return None
    try:
        from kiteconnect import KiteConnect
        kc = KiteConnect(api_key=c["api_key"])
        kc.set_access_token(c["access_token"])
        _kite = kc
        return kc
    except Exception as e:
        log.warning(f"Kite client init failed: {e}")
        return None


def _current_month_futures(kc, symbols: List[str]) -> Dict[str, str]:
    """Map each underlying symbol -> its NEAREST-expiry FUT tradingsymbol on NFO.

    Kite's instrument dump lists every contract; we pick, per underlying, the
    nearest non-expired monthly future. Cached for the process lifetime (the
    contract set only changes at monthly expiry).
    """
    global _fut_map
    if _fut_map:
        return _fut_map
    try:
        instruments = kc.instruments("NFO")
    except Exception as e:
        log.error(f"Kite instruments() failed: {e}")
        return {}
    want = set(s.upper() for s in symbols)
    today = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).date()
    best: Dict[str, tuple] = {}   # sym -> (expiry_date, tradingsymbol)
    for ins in instruments:
        if ins.get("instrument_type") != "FUT":
            continue
        name = (ins.get("name") or "").upper()
        if name not in want:
            continue
        exp = ins.get("expiry")
        if not exp:
            continue
        exp_d = exp if hasattr(exp, "year") else datetime.strptime(str(exp), "%Y-%m-%d").date()
        if exp_d < today:
            continue
        if name not in best or exp_d < best[name][0]:
            best[name] = (exp_d, ins["tradingsymbol"])
    _fut_map = {sym: ts for sym, (_, ts) in best.items()}
    log.info(f"Kite: mapped {len(_fut_map)} current-month futures")
    return _fut_map


def get_oi_snapshots(symbols: List[str], indices: List[str] = None) -> Dict[str, Dict]:
    """LIVE OI + price for the given F&O stocks (+ indices) via Kite futures quotes.

    Returns {symbol: {"symbol","total_oi","underlying_value","last_price",
    "oi","volume","source":"kite"}}. Empty dict if Kite unavailable so callers
    can fall back to the (silent) scraper.
    """
    kc = _client()
    if kc is None:
        return {}
    all_syms = list(symbols) + list(indices or INDEX_ROOTS)
    fut = _current_month_futures(kc, all_syms)
    if not fut:
        return {}
    # Build "NFO:TRADINGSYMBOL" keys for a single batched quote() call.
    q_keys = {sym: f"NFO:{ts}" for sym, ts in fut.items()}
    try:
        data = kc.quote(list(q_keys.values()))
    except Exception as e:
        log.error(f"Kite quote() failed: {e}")
        return {}
    out: Dict[str, Dict] = {}
    for sym, key in q_keys.items():
        d = data.get(key)
        if not d:
            continue
        oi = d.get("oi", 0) or 0
        last = d.get("last_price", 0) or 0
        if oi <= 0 or last <= 0:
            continue
        out[sym] = {
            "symbol": sym,
            "total_oi": oi,               # futures OI (contracts) — buildup metric
            "underlying_value": last,     # futures last price ~ underlying (proxy)
            "last_price": last,
            "oi": oi,
            "volume": d.get("volume", 0) or 0,
            "source": "kite",
        }
    log.info(f"Kite OI snapshots: {len(out)}/{len(all_syms)} (live futures OI)")
    return out


# ─────────────────────────────────────────────────────────────────────
#  One-time daily auth helpers (run manually each morning, or via TOTP)
# ─────────────────────────────────────────────────────────────────────

def login_url() -> str:
    """Print the Kite login URL. Open it, log in, and copy the request_token
    from the redirect URL, then call exchange_token(request_token)."""
    from kiteconnect import KiteConnect
    kc = KiteConnect(api_key=os.environ.get("KITE_API_KEY", ""))
    url = kc.login_url()
    print(f"1) Open this URL and log in:\n   {url}\n"
          "2) After login you'll be redirected to a URL containing "
          "`request_token=XXXX`.\n"
          "3) Run: python3 -c \"import zerodha_source as z; "
          "z.exchange_token('XXXX')\"")
    return url


def exchange_token(request_token: str) -> Optional[str]:
    """Swap a one-time request_token for a daily access_token and print it so it
    can be pasted into .env as KITE_ACCESS_TOKEN."""
    from kiteconnect import KiteConnect
    kc = KiteConnect(api_key=os.environ.get("KITE_API_KEY", ""))
    try:
        sess = kc.generate_session(
            request_token, api_secret=os.environ.get("KITE_API_SECRET", ""))
        tok = sess["access_token"]
        print(f"\nKITE_ACCESS_TOKEN={tok}\n\nPaste that into .env (replace the old "
              "one). Valid until ~6am tomorrow IST.")
        return tok
    except Exception as e:
        print(f"Token exchange failed: {e}")
        return None
