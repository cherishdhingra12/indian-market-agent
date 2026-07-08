"""
Alert deduplication database.
Stores seen signal hashes so the same signal doesn't fire twice.

Uses SQLite (built-in, no extra dependencies).
"""

import hashlib
import json
import logging
import sqlite3
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alerts.db")


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=OFF")
    return conn


def init_db():
    """Create the alerts table if it doesn't exist."""
    conn = _get_conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS detected_signals (
                signal_hash TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                notified INTEGER DEFAULT 1
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_signals_symbol
            ON detected_signals(symbol)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_signals_timestamp
            ON detected_signals(timestamp)
        """)
        conn.commit()
        log.info(f"Alert DB initialized at {DB_PATH}")
    except Exception as e:
        log.error(f"Alert DB init failed: {e}")
    finally:
        conn.close()


# Volatile keys that must NEVER contribute to the dedup identity.
# (Timestamps, presentation flags, and free-text prose all change between
#  otherwise-identical signals and would defeat deduplication.)
_VOLATILE_KEYS = {
    "_time", "_ts", "_format",
    "reason", "reasons", "entry_hint", "exit_hint",
    "sources", "signal_types", "company",
}

# Numeric metrics that define a signal's "reading". Rounded into coarse
# buckets so a materially-new reading re-fires, but tiny jitter does not.
_METRIC_BUCKETS = {
    "oi_change_pct": 5.0,      # nearest 5%
    "price_change_pct": 1.0,   # nearest 1%
    "delivery_pct": 5.0,       # nearest 5%
    "change": 5.0,             # nearest 5%
    "signal_count": 1.0,       # exact
    "value": 1.0,              # index level (VIX/USDINR), nearest whole unit
}


def _ist_date() -> str:
    """Current date in IST — dedup resets each trading day."""
    from datetime import datetime, timezone, timedelta
    return (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d")


def _identity_payload(data: dict) -> dict:
    """
    Extract the stable identity of a signal for deduplication.
    Uses direction/confidence/action plus coarse-bucketed numeric metrics,
    ignoring volatile keys (timestamps, prose, presentation flags).
    """
    payload = {}
    # Coarse-bucketed numeric metrics
    for key, bucket in _METRIC_BUCKETS.items():
        if key in data and isinstance(data.get(key), (int, float)):
            payload[key] = round(float(data[key]) / bucket) * bucket
    # Low-cardinality categorical identity fields
    for key in ("action", "confidence", "direction"):
        val = data.get(key)
        if val not in (None, ""):
            payload[key] = val
    return payload


def _make_hash(symbol: str, signal_type: str, data: dict) -> str:
    """
    Create a deterministic dedup hash from the signal's STABLE identity only.

    Signature = symbol | signal_type | IST-date | coarse metric bucket.
    Volatile fields (_time, _format, reason, entry_hint, ...) are excluded so
    the same underlying event does not re-fire every poll cycle. A materially
    different reading (metric crosses a bucket boundary) or a new trading day
    produces a new hash and is allowed to fire again.
    """
    payload = _identity_payload(data)
    raw = f"{symbol}|{signal_type}|{_ist_date()}|{json.dumps(payload, sort_keys=True, default=str)}"
    return hashlib.sha256(raw.encode()).hexdigest()


def was_notified(symbol: str, signal_type: str, data: dict) -> bool:
    """Check if this exact signal has already been sent."""
    signal_hash = _make_hash(symbol, signal_type, data)
    conn = _get_conn()
    try:
        cursor = conn.execute(
            "SELECT 1 FROM detected_signals WHERE signal_hash = ?",
            (signal_hash,),
        )
        return cursor.fetchone() is not None
    except Exception as e:
        log.error(f"Alert DB query failed: {e}")
        return False
    finally:
        conn.close()


def mark_notified(symbol: str, signal_type: str, data: dict) -> str:
    """Mark a signal as notified so it won't fire again."""
    signal_hash = _make_hash(symbol, signal_type, data)
    # Always store a real timestamp; some signals (e.g. predictive alerts) never
    # carry a "_time" and an empty value would be purged by cleanup_old_entries,
    # letting the same signal re-fire the same day.
    ts = data.get("_time") or datetime.now(timezone.utc).isoformat()
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO detected_signals (signal_hash, symbol, signal_type, timestamp) VALUES (?, ?, ?, ?)",
            (signal_hash, symbol, signal_type, ts),
        )
        conn.commit()
        return signal_hash
    except Exception as e:
        log.error(f"Alert DB insert failed: {e}")
        return signal_hash
    finally:
        conn.close()


def cleanup_old_entries(days: int = 7):
    """Remove signal entries older than N days."""
    conn = _get_conn()
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cursor = conn.execute(
            "DELETE FROM detected_signals WHERE timestamp < ?",
            (cutoff,),
        )
        conn.commit()
        log.info(f"Alert DB cleanup: removed {cursor.rowcount} old entries")
    except Exception as e:
        log.error(f"Alert DB cleanup failed: {e}")
    finally:
        conn.close()


def get_stats() -> dict:
    """Get statistics about stored alerts."""
    conn = _get_conn()
    try:
        total = conn.execute("SELECT COUNT(*) FROM detected_signals").fetchone()[0]
        return {"total_signals": total}
    except Exception as e:
        log.error(f"Alert DB stats failed: {e}")
        return {"total_signals": 0}
    finally:
        conn.close()
