"""
Insider / Early-Signal News Scrapers for Indian Markets.
Collects exchange filings and regulatory data BEFORE they hit mainstream news.

Sources (all public, all legal):
  1. NSE Bulk Deals            — Large trades (₹5L+) executed in bulk
  2. NSE Block Deals           — Large trades (₹5Cr+) on block basis
  3. NSE Corporate Filings     — Insider trading, SAST, pledges, credit ratings
  4. BSE Corporate Announcements — BSE-listed company filings
  5. BSE Insider Trading       — BSE-listed insider trades
  6. SEBI Orders               — SEBI investigations, penalties, show-cause notices
  7. PIB Press Releases        — Government policy, budget, sector announcements
"""

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional
from urllib.parse import urljoin, urlparse, parse_qs
from html import unescape

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/125.0.6422.80 Mobile/15E148 Safari/604.1",
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


def _fetch_soup(url: str, timeout: int = 15,
                session: Optional[requests.Session] = None) -> Optional[BeautifulSoup]:
    own_session = False
    if session is None:
        session = _session()
        own_session = True
    try:
        resp = session.get(url, timeout=timeout)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "lxml")
    except Exception as e:
        log.warning(f"Fetch soup failed [{url}]: {e}")
        return None
    finally:
        if own_session:
            session.close()


def _nse_session() -> Optional[requests.Session]:
    try:
        session = _session()
        session.get("https://www.nseindia.com", timeout=10)
        time.sleep(0.5)
        return session
    except Exception as e:
        log.warning(f"NSE session init failed: {e}")
        return None


def _nse_corp_announcements(session: requests.Session) -> List[Dict]:
    """Fetch NSE corporate announcements and return raw items."""
    try:
        resp = session.get(
            "https://www.nseindia.com/api/corporate-announcements?index=equities",
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        log.warning(f"NSE corp announcements fetch failed: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────
#  1. NSE Bulk Deals
#  2. NSE Block Deals
# ─────────────────────────────────────────────────────────────────────
# Correct endpoint: /api/snapshot-capital-market-largedeal
# Returns dict with BULK_DEALS_DATA, BLOCK_DEALS_DATA, SHORT_DEALS_DATA

def _scrape_nse_large_deals() -> Dict[str, List[Dict]]:
    """
    Fetch NSE large deals (bulk + block) from the snapshot API.
    Returns {"bulk": [...], "block": [...]}.
    """
    result = {"bulk": [], "block": []}
    session = _nse_session()
    if not session:
        return result
    try:
        resp = session.get(
            "https://www.nseindia.com/api/snapshot-capital-market-largedeal",
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            return result

        for item in data.get("BULK_DEALS_DATA", []):
            if isinstance(item, dict) and item.get("symbol"):
                result["bulk"].append({
                    "title": f"{item['symbol']}: Bulk Deal — {item.get('clientName', 'Unknown')} {item.get('buySell', '')}",
                    "url": f"https://www.nseindia.com/get-quotes/equity?symbol={item['symbol']}",
                    "source": "NSE Bulk Deal",
                    "snippet": f"{item.get('name', '')} | Qty: {item.get('qty', '')} @ ₹{item.get('watp', '')} | {item.get('date', '')}",
                    "symbol": item["symbol"],
                    "deal_type": "bulk",
                })

        for item in data.get("BLOCK_DEALS_DATA", []):
            if isinstance(item, dict) and item.get("symbol"):
                result["block"].append({
                    "title": f"{item['symbol']}: Block Deal — {item.get('clientName', 'Unknown')} {item.get('buySell', '')}",
                    "url": f"https://www.nseindia.com/get-quotes/equity?symbol={item['symbol']}",
                    "source": "NSE Block Deal",
                    "snippet": f"{item.get('name', '')} | Qty: {item.get('qty', '')} @ ₹{item.get('watp', '')} | {item.get('date', '')}",
                    "symbol": item["symbol"],
                    "deal_type": "block",
                })

        log.info(f"NSE Large Deals: {len(result['bulk'])} bulk, {len(result['block'])} block")
    except Exception as e:
        log.error(f"NSE large deals scrape failed: {e}")
    finally:
        session.close()

    return result


def scrape_nse_bulk_deals() -> List[Dict]:
    deals = _scrape_nse_large_deals()
    return deals["bulk"]


def scrape_nse_block_deals() -> List[Dict]:
    deals = _scrape_nse_large_deals()
    return deals["block"]


# ─────────────────────────────────────────────────────────────────────
#  3. NSE Corporate Filings (Insider Trading / SAST / Pledge / Credit Ratings)
# ─────────────────────────────────────────────────────────────────────
# All extracted from the same NSE corporate announcements API by keyword matching.

def _nse_corp_filings_by_keywords(
    source_label: str,
    keyword_groups: List[List[str]],
    signal_label: Optional[str] = None,
) -> List[Dict]:
    """
    Extract corporate announcements matching ANY of the keyword groups.
    Each group is OR'd; groups are AND'd (any group must match).
    """
    articles = []
    session = _nse_session()
    if not session:
        return articles
    try:
        items = _nse_corp_announcements(session)
        for item in items:
            text = (item.get("attchmntText") or "").strip()
            desc = (item.get("desc") or "").strip()
            symbol = (item.get("symbol") or "").strip()
            company = (item.get("sm_name") or "").strip()
            pdf = (item.get("attchmntFile") or "").strip()
            timestamp = (item.get("an_dt") or "").strip()
            combined = f"{text} {desc}".lower()

            if not any(all(kw.lower() in combined for kw in group) for group in keyword_groups):
                continue

            title = f"{symbol or company}: {desc}" if symbol or company else desc
            snippet_parts = []
            if company:
                snippet_parts.append(company)
            if text:
                snippet_parts.append(text[:200])
            if timestamp:
                snippet_parts.append(timestamp)

            article = {
                "title": title[:250],
                "url": pdf if pdf else f"https://www.nseindia.com/get-quotes/equity?symbol={symbol}" if symbol else "",
                "source": source_label,
                "snippet": " | ".join(snippet_parts)[:300],
                "symbol": symbol,
            }
            if signal_label:
                article["signal"] = signal_label
            articles.append(article)

        log.info(f"{source_label}: {len(articles)} items")
    except Exception as e:
        log.error(f"{source_label} scrape failed: {e}")
    finally:
        session.close()

    return articles


INSIDER_KEYWORDS = [
    ["prohibition of insider trading"],
    ["pit regulations"],
    ["regulation 7(2)"],
    ["insider trading"],
    ["insider"],
]
SAST_KEYWORDS = [
    ["substantial acquisition"],
    ["takeover"],
    ["sast"],
    ["open offer"],
    ["regulation 31"],
    ["acquirer"],
]
PLEDGE_KEYWORDS = [
    ["pledge"],
    ["pledged"],
    ["pledging"],
    ["invocation of pledge"],
]
RATING_KEYWORDS = [
    ["credit rating"],
    ["rating upgrade"],
    ["rating downgrade"],
    ["rating revision"],
    ["rating reaffirmed"],
    ["rating assigned"],
    ["crisil"],
    ["icra"],
    ["care rating"],
    ["brickwork"],
    ["acuité"],
    ["fitch"],
    ["rating action"],
    ["rating outlook"],
]


def scrape_nse_insider_trading() -> List[Dict]:
    return _nse_corp_filings_by_keywords("NSE Insider Trade", INSIDER_KEYWORDS)


def scrape_nse_sast() -> List[Dict]:
    return _nse_corp_filings_by_keywords("NSE SAST", SAST_KEYWORDS)


def scrape_nse_pledge() -> List[Dict]:
    return _nse_corp_filings_by_keywords("NSE Pledge", PLEDGE_KEYWORDS)


def scrape_nse_credit_ratings() -> List[Dict]:
    return _nse_corp_filings_by_keywords("NSE Rating Change", RATING_KEYWORDS)


# ─────────────────────────────────────────────────────────────────────
#  4. BSE Corporate Announcements
# ─────────────────────────────────────────────────────────────────────

def scrape_bse_announcements() -> List[Dict]:
    """
    Scrape BSE announcements page via HTML.
    Falls back to fetching the main announcements page.
    """
    articles = []
    try:
        session = _session()
        session.headers.update({
            "Referer": "https://www.bseindia.com/",
            "Accept": "text/html,application/xhtml+xml",
        })
        session.get("https://www.bseindia.com", timeout=10)
        time.sleep(0.5)

        soup = _fetch_soup(
            "https://www.bseindia.com/markets/equity/EQReports/Corporate_announcements.html",
            timeout=20, session=session,
        )
        if not soup:
            # Try the market dashboard
            soup = _fetch_soup(
                "https://www.bseindia.com/markets/MarketNew.html",
                timeout=20, session=session,
            )
        if not soup:
            log.warning("BSE announcements: page not accessible")
            return articles

        for link in soup.select("a[href*='announcement'], a[href*='Announcement'], a[href*='corp']"):
            href = link.get("href", "")
            text = link.get_text(strip=True)
            if text and len(text) > 15:
                url = href if href.startswith("http") else f"https://www.bseindia.com{href}" if href.startswith("/") else href
                articles.append({
                    "title": text[:250],
                    "url": url,
                    "source": "BSE Filing",
                    "snippet": "",
                })

        # Also try to find tables
        for table in soup.select("table"):
            for row in table.select("tr"):
                cells = row.select("td")
                if len(cells) >= 2:
                    title = cells[0].get_text(strip=True)
                    link_el = cells[0].select_one("a")
                    href = link_el.get("href", "") if link_el else ""
                    snippet = cells[1].get_text(strip=True)[:200] if len(cells) > 1 else ""
                    if title and len(title) > 15:
                        url = href if href.startswith("http") else f"https://www.bseindia.com{href}" if href.startswith("/") else href
                        articles.append({
                            "title": title[:250],
                            "url": url,
                            "source": "BSE Filing",
                            "snippet": snippet,
                        })

        log.info(f"BSE Announcements: {len(articles)} items")
    except Exception as e:
        log.error(f"BSE announcements scrape failed: {e}")

    return articles


# ─────────────────────────────────────────────────────────────────────
#  5. BSE Insider Trading
# ─────────────────────────────────────────────────────────────────────

def scrape_bse_insider_trading() -> List[Dict]:
    """
    BSE Insider Trading disclosures — scrape from BSE insider trading page.
    """
    articles = []
    try:
        session = _session()
        session.headers.update({
            "Referer": "https://www.bseindia.com/",
            "Accept": "text/html,application/xhtml+xml",
        })
        session.get("https://www.bseindia.com", timeout=10)
        time.sleep(0.5)

        soup = _fetch_soup(
            "https://www.bseindia.com/markets/equity/EQReports/InsiderTrading.html",
            timeout=20, session=session,
        )
        if not soup:
            log.warning("BSE insider trading: page not accessible")
            return articles

        for table in soup.select("table"):
            for row in table.select("tr"):
                cells = row.select("td")
                if len(cells) >= 2:
                    text = cells[0].get_text(strip=True)
                    link_el = cells[0].select_one("a")
                    href = link_el.get("href", "") if link_el else ""
                    snippet = cells[1].get_text(strip=True)[:200] if len(cells) > 1 else ""
                    if text and len(text) > 15:
                        url = href if href.startswith("http") else f"https://www.bseindia.com{href}" if href.startswith("/") else href
                        articles.append({
                            "title": text[:250],
                            "url": url,
                            "source": "BSE Insider Trade",
                            "snippet": snippet,
                        })

        log.info(f"BSE Insider Trading: {len(articles)} items")
    except Exception as e:
        log.error(f"BSE insider trading scrape failed: {e}")

    return articles


# ─────────────────────────────────────────────────────────────────────
#  6. SEBI Orders Database
# ─────────────────────────────────────────────────────────────────────

def scrape_sebi_orders() -> List[Dict]:
    """
    SEBI Orders — investigations, show-cause notices, penalties, bans.
    Parsed from SEBI's RSS feed which includes enforcement orders.
    """
    return _parse_rss_lenient("https://www.sebi.gov.in/sebirss.xml", "SEBI Order")


# ─────────────────────────────────────────────────────────────────────
#  7. PIB Press Releases
# ─────────────────────────────────────────────────────────────────────

def _parse_rss_lenient(url: str, source_name: str) -> List[Dict]:
    """Fetch and parse RSS with lenient parsing (handles malformed XML)."""
    import xml.etree.ElementTree as ET
    import io

    articles = []
    try:
        session = _session()
        resp = session.get(url, timeout=10)
        resp.raise_for_status()
        raw = resp.text
    except Exception as e:
        log.warning(f"RSS fetch failed for {source_name}: {e}")
        return articles

    # Try to fix common PIB RSS issues: stray & characters, invalid XML entities
    raw = re.sub(r'&(?!(?:amp|lt|gt|quot|apos|#\d+);)', '&amp;', raw)

    try:
        root = ET.parse(io.BytesIO(raw.encode("utf-8"))).getroot()
    except ET.ParseError as e:
        log.warning(f"RSS parse error for {source_name}: {e}")
        return articles

    for item in root.iter("item"):
        title_el = item.find("title")
        link_el = item.find("link")
        desc_el = item.find("description")
        title = title_el.text.strip() if title_el is not None and title_el.text else None
        link = link_el.text.strip() if link_el is not None and link_el.text else None
        desc = desc_el.text.strip() if desc_el is not None and desc_el.text else None
        if title and link:
            articles.append({
                "title": unescape(title),
                "url": link.strip(),
                "source": source_name,
                "snippet": unescape(desc)[:300] if desc else "",
            })

    log.info(f"RSS {source_name}: {len(articles)} articles")
    return articles


def _scrape_pib_html() -> List[Dict]:
    """
    Fallback: scrape PIB press releases from the index page.
    PIB's RSS feeds are broken (return HTML), so we scrape the main page.
    """
    articles = []
    try:
        soup = _fetch_soup("https://pib.gov.in/indexd.aspx", timeout=15)
        if not soup:
            return articles

        for link in soup.select("a[href*='PRID'], a[href*='Release'], a[href*='PressRelease']"):
            href = link.get("href", "")
            text = link.get_text(strip=True)
            if text and len(text) > 15:
                url = href if href.startswith("http") else f"https://pib.gov.in/{href.lstrip('/')}" if href.startswith("/") else href
                articles.append({
                    "title": text[:250],
                    "url": url,
                    "source": "PIB Release",
                    "snippet": "",
                })
        log.info(f"PIB Releases (HTML): {len(articles)} items")
    except Exception as e:
        log.error(f"PIB HTML scrape failed: {e}")

    return articles


def scrape_pib_releases() -> List[Dict]:
    """
    PIB (Press Information Bureau) press releases.
    Government policy, budget, tariff, sector announcements.
    PIB RSS feeds are broken (return HTML), so we scrape the main page.
    """
    return _scrape_pib_html()


# ─────────────────────────────────────────────────────────────────────
#  Insider News Collector — Master orchestrator
# ─────────────────────────────────────────────────────────────────────

INSIDER_SCRAPERS = {
    "nse_bulk_deals": scrape_nse_bulk_deals,
    "nse_block_deals": scrape_nse_block_deals,
    "nse_insider_trading": scrape_nse_insider_trading,
    "nse_sast": scrape_nse_sast,
    "nse_pledge": scrape_nse_pledge,
    "bse_announcements": scrape_bse_announcements,
    "bse_insider_trading": scrape_bse_insider_trading,
    "sebi_orders": scrape_sebi_orders,
    "pib_releases": scrape_pib_releases,
    "nse_credit_ratings": scrape_nse_credit_ratings,
}


def collect_insider_news(config: dict) -> List[Dict]:
    """
    Collect early-signal news from all insider sources.
    Each scraper runs independently; failures don't block others.
    """
    all_articles = []
    sources_enabled = config.get("insider_sources", {})

    for source_name, scraper_fn in INSIDER_SCRAPERS.items():
        if not sources_enabled.get(source_name, True):
            log.info(f"Insider source '{source_name}' disabled — skipping")
            continue
        try:
            articles = scraper_fn()
            all_articles.extend(articles)
            time.sleep(config.get("request_delay", 1.0))
        except Exception as e:
            log.error(f"Insider scraper '{source_name}' failed: {e}")

    seen_urls = set()
    deduped = []
    for a in all_articles:
        url = a.get("url", "").rstrip("/")
        norm = url.lower().split("?")[0].split("#")[0]
        if norm and norm not in seen_urls and len(a.get("title", "")) > 10:
            seen_urls.add(norm)
            deduped.append(a)

    log.info(f"Insider news: {len(deduped)} unique articles from {len(all_articles)} total")
    return deduped
