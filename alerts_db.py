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


def _make_hash(symbol: str, signal_type: str, data: dict) -> str:
    """Create a deterministic hash for a signal."""
    raw = f"{symbol}|{signal_type}|{json.dumps(data, sort_keys=True, default=str)}"
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
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO detected_signals (signal_hash, symbol, signal_type, timestamp) VALUES (?, ?, ?, ?)",
            (signal_hash, symbol, signal_type, data.get("_time", "")),
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
        from datetime import datetime, timedelta, timezone

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
