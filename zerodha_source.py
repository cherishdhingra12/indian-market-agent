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
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

_TOKEN_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".kite_token")

# Index futures we also want buildup signals on (Kite tradingsymbols use these roots)
INDEX_ROOTS = ["NIFTY", "BANKNIFTY"]

# Bot symbol -> Kite/NSE futures "name" where they differ (renames / punctuation).
# Symbols with NO current F&O future (dropped from the F&O list) are simply not
# mapped and skipped: TATAMOTORS, BERGEPAINT.
SYMBOL_ALIASES = {
    "BAJAJAUTO": "BAJAJ-AUTO",
    "ADANITRANS": "ADANIENSOL",   # renamed Adani Energy Solutions
    "TORPHARMA": "TORNTPHARM",    # Torrent Pharma
}

_kite = None                 # cached KiteConnect client
_fut_map: Dict[str, str] = {}   # symbol -> current-month FUT tradingsymbol
_token_map: Dict[str, int] = {}  # "NFO:TRADINGSYMBOL" -> instrument_token (unused for quote())


def _ist_today() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d")


def _login_creds() -> Optional[dict]:
    """Credentials for autonomous auto-login (user id, password, TOTP secret)."""
    uid = os.environ.get("KITE_USER_ID", "")
    pwd = os.environ.get("KITE_PASSWORD", "")
    totp = os.environ.get("KITE_TOTP_SECRET", "")
    if uid and pwd and totp and "YOUR_" not in uid:
        return {"user_id": uid, "password": pwd, "totp_secret": totp}
    return None


def _cached_token() -> Optional[str]:
    """Return today's cached access_token if present (Kite tokens last one day)."""
    try:
        with open(_TOKEN_CACHE) as f:
            d = json.load(f)
        if d.get("date") == _ist_today() and d.get("access_token"):
            return d["access_token"]
    except Exception:
        pass
    return None


def _save_token(tok: str):
    try:
        with open(_TOKEN_CACHE, "w") as f:
            json.dump({"date": _ist_today(), "access_token": tok}, f)
    except Exception as e:
        log.warning(f"Could not cache Kite token: {e}")


def auto_login() -> Optional[str]:
    """Generate a fresh access_token via Zerodha login + TOTP (no manual step).

    Uses the same endpoints the browser login uses:
      1. POST /api/login (user_id + password) -> request_id
      2. POST /api/twofa (request_id + current TOTP) -> authenticated session
      3. GET connect/login -> redirects with request_token
      4. exchange request_token + api_secret -> access_token
    Needs KITE_USER_ID, KITE_PASSWORD, KITE_TOTP_SECRET, KITE_API_KEY/SECRET.
    """
    lc = _login_creds()
    key = os.environ.get("KITE_API_KEY", "")
    secret = os.environ.get("KITE_API_SECRET", "")
    if not lc or not key or not secret:
        return None
    try:
        import requests
        import pyotp
        from urllib.parse import urlparse, parse_qs
        from kiteconnect import KiteConnect

        s = requests.Session()
        r = s.post("https://kite.zerodha.com/api/login",
                   data={"user_id": lc["user_id"], "password": lc["password"]}, timeout=15)
        r.raise_for_status()
        request_id = r.json()["data"]["request_id"]

        totp = pyotp.TOTP(lc["totp_secret"]).now()
        r = s.post("https://kite.zerodha.com/api/twofa",
                   data={"user_id": lc["user_id"], "request_id": request_id,
                         "twofa_value": totp, "twofa_type": "totp"}, timeout=15)
        r.raise_for_status()

        # Trigger the Connect login redirect; capture request_token from the
        # redirect URL (the final hop to the app redirect_url may be unreachable,
        # so read it from history/exception rather than requiring a live endpoint).
        request_token = None
        try:
            resp = s.get(f"https://kite.zerodha.com/connect/login?api_key={key}&v=3",
                         allow_redirects=True, timeout=15)
            for h in list(resp.history) + [resp]:
                q = parse_qs(urlparse(h.url).query)
                if "request_token" in q:
                    request_token = q["request_token"][0]; break
        except Exception as e:
            q = parse_qs(urlparse(str(getattr(e, "request", "") ) or "").query)
            if "request_token" in q:
                request_token = q["request_token"][0]
        if not request_token:
            log.error("Kite auto-login: could not capture request_token")
            return None

        kc = KiteConnect(api_key=key)
        sess = kc.generate_session(request_token, api_secret=secret)
        tok = sess["access_token"]
        _save_token(tok)
        log.info("Kite auto-login succeeded; access_token refreshed")
        return tok
    except Exception as e:
        log.error(f"Kite auto-login failed: {e}")
        return None


def _access_token() -> Optional[str]:
    """Resolve today's access token: explicit env > cache > auto-login."""
    t = os.environ.get("KITE_ACCESS_TOKEN", "")
    if t and "YOUR_" not in t:
        return t
    return _cached_token() or auto_login()


def available() -> bool:
    """True if we have (or can obtain) live Kite access — lets callers fall back."""
    key = os.environ.get("KITE_API_KEY", "")
    if not key or "YOUR_" in key:
        return False
    # A usable token via env/cache, OR the ability to auto-login.
    return bool(_access_token()) if (os.environ.get("KITE_ACCESS_TOKEN")
                                     or _cached_token()) else (_login_creds() is not None)


def _client():
    """Return an authenticated KiteConnect client, or None if unavailable."""
    global _kite
    if _kite is not None:
        return _kite
    key = os.environ.get("KITE_API_KEY", "")
    tok = _access_token()
    if not key or "YOUR_" in key or not tok:
        return None
    try:
        from kiteconnect import KiteConnect
        kc = KiteConnect(api_key=key)
        kc.set_access_token(tok)
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
    # Map Kite's futures "name" back to the bot's symbol (via aliases where they differ)
    alias_to_sym = {}
    for s in want:
        alias_to_sym[SYMBOL_ALIASES.get(s, s)] = s
    match_names = set(alias_to_sym)
    today = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).date()
    best: Dict[str, tuple] = {}   # bot_sym -> (expiry_date, tradingsymbol)
    for ins in instruments:
        if ins.get("instrument_type") != "FUT":
            continue
        name = (ins.get("name") or "").upper()
        if name not in match_names:
            continue
        sym = alias_to_sym[name]      # back to the bot's symbol
        exp = ins.get("expiry")
        if not exp:
            continue
        exp_d = exp if hasattr(exp, "year") else datetime.strptime(str(exp), "%Y-%m-%d").date()
        if exp_d < today:
            continue
        if sym not in best or exp_d < best[sym][0]:
            best[sym] = (exp_d, ins["tradingsymbol"])
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
