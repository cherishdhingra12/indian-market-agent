#!/usr/bin/env python3
"""
Indian Financial Market News Agent
Researches the web for breaking news in the Indian financial sector,
ranks them by stock market impact using AI, and sends top news via Telegram.

Schedule: 9:00 AM (pre-market), 2:00 PM (mid-session), 7:00 PM (post-market)

Architecture:
  1. Primary: RSS feeds from major Indian financial news sites
  2. Secondary: HTML scraping of news sites
  3. Fallback: DuckDuckGo web search for breaking news
  4. AI analysis: LLM ranks articles by market impact (Groq/OpenAI/Anthropic)
  5. Delivery: Telegram bot sends top news
"""

import os
import sys
import json
import time
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, date, timezone, timedelta
from typing import List, Dict, Optional, Tuple
from urllib.parse import urljoin, urlparse, parse_qs
from html import unescape

import requests
from bs4 import BeautifulSoup

from insider_scrapers import collect_insider_news

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "agent.log")),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────
#  Configuration
# ──────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    """Load configuration from config.py module."""
    try:
        sys.path.insert(0, BASE_DIR)
        import config as cfg
        return {
            "llm_provider": getattr(cfg, "LLM_PROVIDER", "none"),
            "llm_api_key": getattr(cfg, "LLM_API_KEY", ""),
            "llm_api_base": getattr(cfg, "LLM_API_BASE", ""),
            "llm_model": getattr(cfg, "LLM_MODEL", ""),
            "llm_temperature": getattr(cfg, "LLM_TEMPERATURE", 0.2),
            "llm_max_tokens": getattr(cfg, "LLM_MAX_TOKENS", 2000),
            "telegram_bot_token": getattr(cfg, "TELEGRAM_BOT_TOKEN", ""),
            "telegram_chat_id": getattr(cfg, "TELEGRAM_CHAT_ID", ""),
            "top_news_count": getattr(cfg, "TOP_NEWS_COUNT", 7),
            "min_impact_score": getattr(cfg, "MIN_IMPACT_SCORE", 5),
            "request_delay": getattr(cfg, "REQUEST_DELAY", 1.5),
            "news_sources": getattr(cfg, "NEWS_SOURCES", {}),
            "insider_sources": getattr(cfg, "INSIDER_SOURCES", {}),
            "insider_top_news_count": getattr(cfg, "INSIDER_TOP_NEWS_COUNT", 10),
            "today": getattr(cfg, "TODAY", date.today().strftime("%B %d, %Y")),
            "schedule_label": getattr(cfg, "SCHEDULE_LABEL", {}),
        }
    except Exception as e:
        log.warning(f"Could not load config.py: {e}. Using defaults + env vars.")
        return _default_config()


def _default_config() -> dict:
    """Fallback default configuration."""
    return {
        "llm_provider": os.environ.get("LLM_PROVIDER", "groq"),
        "llm_api_key": os.environ.get("GROQ_API_KEY", ""),
        "llm_api_base": os.environ.get("LLM_API_BASE", "https://api.groq.com/openai/v1/chat/completions"),
        "llm_model": os.environ.get("LLM_MODEL", "llama-3.3-70b-versatile"),
        "llm_temperature": float(os.environ.get("LLM_TEMPERATURE", "0.2")),
        "llm_max_tokens": int(os.environ.get("LLM_MAX_TOKENS", "2000")),
        "telegram_bot_token": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""),
        "top_news_count": int(os.environ.get("TOP_NEWS_COUNT", "7")),
        "min_impact_score": int(os.environ.get("MIN_IMPACT_SCORE", "5")),
        "request_delay": float(os.environ.get("REQUEST_DELAY", "1.5")),
        "news_sources": {},
        "insider_sources": {},
        "insider_top_news_count": 10,
        "today": date.today().strftime("%B %d, %Y"),
        "schedule_label": {"0900": "9:00 AM", "1400": "2:00 PM", "1900": "7:00 PM"},
    }


# ──────────────────────────────────────────────────────────────────────
#  HTTP Utilities
# ──────────────────────────────────────────────────────────────────────

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/125.0.6422.80 Mobile/15E148 Safari/604.1",
]

_ua_index = 0

def _get_session() -> requests.Session:
    """Create a requests Session with rotating User-Agent."""
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
    # Add a small delay to avoid rate limiting
    time.sleep(0.3)
    return session


def _fetch_soup(url: str, timeout: int = 15) -> Optional[BeautifulSoup]:
    """Fetch URL and return BeautifulSoup object."""
    try:
        session = _get_session()
        resp = session.get(url, timeout=timeout)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "lxml")
    except Exception as e:
        log.warning(f"Failed to fetch {url}: {e}")
        return None


def _fetch_text(url: str, timeout: int = 15) -> Optional[str]:
    """Fetch URL and return raw text."""
    try:
        session = _get_session()
        resp = session.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        log.warning(f"Failed to fetch text from {url}: {e}")
        return None


def _fetch_bytes(url: str, timeout: int = 15) -> Optional[bytes]:
    """Fetch URL and return raw bytes (preserves original encoding)."""
    try:
        session = _get_session()
        resp = session.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        log.warning(f"Failed to fetch bytes from {url}: {e}")
        return None


# ──────────────────────────────────────────────────────────────────────
#  RSS Feed Parser (most reliable source)
# ──────────────────────────────────────────────────────────────────────

def _parse_rss(url: str, source_name: str) -> List[Dict]:
    """Fetch and parse an RSS/Atom feed, returning article dicts."""
    articles = []
    raw_bytes = _fetch_bytes(url, timeout=10)
    if not raw_bytes:
        return articles

    try:
        import io
        root = ET.parse(io.BytesIO(raw_bytes)).getroot()
    except ET.ParseError as e:
        log.warning(f"RSS parse error for {source_name}: {e}")
        return articles

    # Try RSS 2.0 format
    for item in root.iter("item"):
        title = _rss_text(item, "title")
        link = _rss_text(item, "link")
        desc = _rss_text(item, "description")
        if title and link:
            articles.append({
                "title": unescape(title),
                "url": link.strip(),
                "source": source_name,
                "snippet": unescape(desc)[:300] if desc else "",
            })

    # Try Atom format
    if not articles:
        for entry in root.iter("{http://www.w3.org/2005/Atom}entry"):
            title = _atom_text(entry, "title")
            link = _atom_link(entry)
            summary = _atom_text(entry, "summary")
            if title and link:
                articles.append({
                    "title": unescape(title),
                    "url": link.strip(),
                    "source": source_name,
                    "snippet": unescape(summary)[:300] if summary else "",
                })

    log.info(f"RSS {source_name}: {len(articles)} articles")
    return articles


def _rss_text(parent, tag: str) -> Optional[str]:
    el = parent.find(tag)
    return el.text.strip() if el is not None and el.text else None


def _atom_text(parent, tag: str) -> Optional[str]:
    el = parent.find(f"{{http://www.w3.org/2005/Atom}}{tag}")
    return el.text.strip() if el is not None and el.text else None


def _atom_link(parent) -> Optional[str]:
    for el in parent.findall("{http://www.w3.org/2005/Atom}link"):
        href = el.get("href")
        if href:
            return href
    return None


# ──────────────────────────────────────────────────────────────────────
#  Source-specific scrapers (HTML)
# ──────────────────────────────────────────────────────────────────────

def scrape_moneycontrol() -> List[Dict]:
    """Scrape Moneycontrol stock news via RSS + HTML fallback."""
    articles = _parse_rss("https://www.moneycontrol.com/rss/business.xml", "Moneycontrol")
    if len(articles) >= 10:
        return articles

    # HTML fallback
    soup = _fetch_soup("https://www.moneycontrol.com/news/business/stocks/")
    if not soup:
        return articles

    for item in soup.select("li, .news_item, article, .story-box"):
        title_el = item.select_one("h2 a, h3 a, a[title], .title a")
        if not title_el:
            continue
        title = title_el.get("title") or title_el.get_text(strip=True)
        link = title_el.get("href", "")
        if not title or not link:
            continue
        snippet_el = item.select_one("p, .desc, .summary")
        snippet = snippet_el.get_text(strip=True) if snippet_el else ""
        articles.append({
            "title": title,
            "url": link if link.startswith("http") else f"https://www.moneycontrol.com{link}",
            "source": "Moneycontrol",
            "snippet": snippet[:300],
        })

    log.info(f"Moneycontrol: {len(articles)} articles")
    return articles


def scrape_economictimes() -> List[Dict]:
    """Scrape ET Markets via RSS + HTML."""
    articles = _parse_rss(
        "https://economictimes.indiatimes.com/rssfeeds/1977021501.cms",
        "Economic Times",
    )
    if len(articles) >= 15:
        return articles

    soup = _fetch_soup("https://economictimes.indiatimes.com/markets/stocks/news")
    if not soup:
        return articles

    for item in soup.select("article, .storyCard, .eachStory, .articleCard, li"):
        title_el = item.select_one("a[title], h3 a, .title a")
        if not title_el:
            continue
        title = title_el.get("title") or title_el.get_text(strip=True)
        link = title_el.get("href", "")
        if not title or not link:
            continue
        snippet_el = item.select_one("p, .desc, .synopsis")
        snippet = snippet_el.get_text(strip=True) if snippet_el else ""
        articles.append({
            "title": title,
            "url": link if link.startswith("http") else f"https://economictimes.indiatimes.com{link}",
            "source": "Economic Times",
            "snippet": snippet[:300],
        })

    log.info(f"Economic Times: {len(articles)} articles")
    return articles


def scrape_business_standard() -> List[Dict]:
    """Scrape Business Standard via RSS (HTML often blocked)."""
    articles = _parse_rss("https://www.business-standard.com/rss/markets-106.rss", "Business Standard")
    if articles:
        return articles

    # Try alternative RSS
    articles = _parse_rss("https://www.business-standard.com/rss/companies-101.rss", "Business Standard")
    if articles:
        return articles

    # HTML fallback (may 403)
    soup = _fetch_soup("https://www.business-standard.com/markets")
    if not soup:
        return articles

    for item in soup.select("article, .story-box, .card, li"):
        title_el = item.select_one("h2 a, h3 a, .title a, a[title]")
        if not title_el:
            continue
        title = title_el.get("title") or title_el.get_text(strip=True)
        link = title_el.get("href", "")
        if not title or not link:
            continue
        snippet_el = item.select_one("p, .desc, .summary")
        snippet = snippet_el.get_text(strip=True) if snippet_el else ""
        articles.append({
            "title": title,
            "url": link if link.startswith("http") else f"https://www.business-standard.com{link}",
            "source": "Business Standard",
            "snippet": snippet[:300],
        })

    log.info(f"Business Standard: {len(articles)} articles")
    return articles


def scrape_livemint() -> List[Dict]:
    """Scrape Livemint via RSS + HTML."""
    articles = _parse_rss("https://www.livemint.com/rss/market", "Livemint")
    if len(articles) >= 10:
        return articles

    soup = _fetch_soup("https://www.livemint.com/market/stock-market-news")
    if not soup:
        return articles

    for item in soup.select("article, div[class*='card'], .story-card, li[class*='story']"):
        title_el = item.select_one("h2 a, h3 a, .headline a, a[class*='headline'], a[data-testid]")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        link = title_el.get("href", "")
        if not title or not link:
            continue
        snippet_el = item.select_one("p, .summary, .subheadline, .desc")
        snippet = snippet_el.get_text(strip=True) if snippet_el else ""
        articles.append({
            "title": title,
            "url": link if link.startswith("http") else f"https://www.livemint.com{link}",
            "source": "Livemint",
            "snippet": snippet[:300],
        })

    log.info(f"Livemint: {len(articles)} articles")
    return articles


def scrape_ndtv_profit() -> List[Dict]:
    """Scrape NDTV Profit via Feedburner RSS + HTML."""
    articles = _parse_rss("https://feeds.feedburner.com/ndtvprofit-latest", "NDTV Profit")
    if len(articles) >= 10:
        return articles

    soup = _fetch_soup("https://www.ndtvprofit.com/markets")
    if not soup:
        return articles

    for item in soup.select("article, .story-card, .card, li"):
        title_el = item.select_one("h2 a, h3 a, .title a, a[class*='title']")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        link = title_el.get("href", "")
        if not title or not link:
            continue
        snippet_el = item.select_one("p, .desc, .summary, .dek")
        snippet = snippet_el.get_text(strip=True) if snippet_el else ""
        articles.append({
            "title": title,
            "url": link if link.startswith("http") else f"https://www.ndtvprofit.com{link}",
            "source": "NDTV Profit",
            "snippet": snippet[:300],
        })

    log.info(f"NDTV Profit: {len(articles)} articles")
    return articles


def scrape_financial_express() -> List[Dict]:
    """Scrape Financial Express via HTML (RSS feeds disabled by site)."""
    soup = _fetch_soup("https://www.financialexpress.com/market/")
    if not soup:
        return []
    articles = []

    for item in soup.select("article, .story-card, .listing-item, .card-item, li"):
        title_el = item.select_one("h2 a, h3 a, .entry-title a, a[rel='bookmark'], a[class*='title']")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        link = title_el.get("href", "")
        if not title or not link:
            continue
        snippet_el = item.select_one("p, .desc, .excerpt, .summary")
        snippet = snippet_el.get_text(strip=True) if snippet_el else ""
        articles.append({
            "title": title,
            "url": link if link.startswith("http") else f"https://www.financialexpress.com{link}",
            "source": "Financial Express",
            "snippet": snippet[:300],
        })

    log.info(f"Financial Express: {len(articles)} articles")
    return articles


def scrape_the_hindu_businessline() -> List[Dict]:
    """Scrape The Hindu BusinessLine via RSS."""
    return _parse_rss("https://www.thehindubusinessline.com/markets/feeder/default.rss", "Hindu BusinessLine")


def scrape_zeebiz() -> List[Dict]:
    """Scrape Zee Business via HTML (RSS behind Akamai CDN)."""
    articles = _parse_rss("https://www.zeebiz.com/rss.xml", "Zee Business")
    if articles:
        return articles

    soup = _fetch_soup("https://www.zeebiz.com/markets")
    if not soup:
        return articles

    for item in soup.select("article, .story-card, .card, li.story-item, .news-card"):
        title_el = item.select_one("h2 a, h3 a, .title a, a[class*='title']")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        link = title_el.get("href", "")
        if not title or not link:
            continue
        snippet_el = item.select_one("p, .desc, .summary, .excerpt")
        snippet = snippet_el.get_text(strip=True) if snippet_el else ""
        articles.append({
            "title": title,
            "url": link if link.startswith("http") else f"https://www.zeebiz.com{link}",
            "source": "Zee Business",
            "snippet": snippet[:300],
        })

    log.info(f"Zee Business: {len(articles)} articles")
    return articles


def scrape_business_today() -> List[Dict]:
    """Scrape Business Today via RSS."""
    articles = _parse_rss("https://www.businesstoday.in/rssfeeds/?id=home", "Business Today")
    return articles


def scrape_inc42() -> List[Dict]:
    """Scrape Inc42 startup funding news via RSS."""
    return _parse_rss("https://inc42.com/feed/", "Inc42")


def scrape_sebi() -> List[Dict]:
    """Scrape SEBI regulatory news via RSS."""
    return _parse_rss("https://www.sebi.gov.in/sebirss.xml", "SEBI")


def scrape_investing_india() -> List[Dict]:
    """Scrape Investing.com India financial news via RSS."""
    return _parse_rss("https://in.investing.com/rss/news.rss", "Investing.com India")


# ──────────────────────────────────────────────────────────────────────
#  High-Value Source: NSE Corporate Announcements (Early Signals)
# ──────────────────────────────────────────────────────────────────────

# Categories that indicate direct stock-moving potential
HIGH_VALUE_CATS = {
    "Outcome of Board Meeting",       # dividends, buybacks, earnings
    "Awarding of order(s)/contract(s)",  # order wins
    "Bagging/Receiving of orders/contracts",  # order wins
    "Appointment",                     # CEO/CFO/Key management changes
    "Analysts/Institutional Investor Meet/Con. Call Updates",  # institutional interest
    "Updates",                         # general updates (may contain price-sensitive info)
    "General Updates",
    "Corrigendum",                     # corrections to previous announcements
}


def scrape_nse_announcements() -> List[Dict]:
    """
    Scrape NSE Corporate Announcements — real-time exchange filings.
    These are filed directly by companies and contain early-stage
    price-sensitive information (order wins, board outcomes, etc.).
    """
    articles = []
    try:
        session = _get_session()
        session.get("https://www.nseindia.com", timeout=10)
        time.sleep(0.5)
        resp = session.get(
            "https://www.nseindia.com/api/corporate-announcements?index=equities",
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            log.warning(f"NSE announcements: unexpected response format")
            return articles

        for item in data:
            cat = (item.get("desc") or "").strip()
            symbol = (item.get("symbol") or "").strip()
            company = (item.get("sm_name") or "").strip()
            text = (item.get("attchmntText") or "").strip()
            pdf = (item.get("attchmntFile") or "").strip()
            timestamp = (item.get("an_dt") or "").strip()

            if not text and not cat:
                continue

            title = f"{symbol}: {cat}" if symbol and cat else text[:120]
            snippet = f"{company} | {text}" if company and text else text
            articles.append({
                "title": title,
                "url": pdf if pdf else f"https://www.nseindia.com/get-quotes/equity?symbol={symbol}",
                "source": "NSE Filing",
                "snippet": snippet[:300],
                "symbol": symbol,
                "category": cat,
                "company": company,
                "timestamp": timestamp,
            })

        log.info(f"NSE Corporate Announcements: {len(articles)} items")
    except Exception as e:
        log.error(f"NSE announcements scrape failed: {e}")

    return articles


def scrape_nse_high_value() -> List[Dict]:
    """
    Scrape NSE announcements and filter for only high-value categories
    that are most likely to move stock prices.
    """
    all_items = scrape_nse_announcements()
    filtered = [a for a in all_items if a.get("category") in HIGH_VALUE_CATS]
    log.info(f"NSE high-value announcements: {len(filtered)} (filtered from {len(all_items)})")
    return filtered


# ──────────────────────────────────────────────────────────────────────
#  Google News RSS — Reliable primary source
# ──────────────────────────────────────────────────────────────────────

GOOGLE_NEWS_RSS = (
    "https://news.google.com/rss/search?"
    "q=indian+stock+market+NSE+BSE+OR+Indian+economy+OR+markets+OR+Sensex+OR+Nifty+OR+RBI+OR+SEBI"
    "&hl=en-IN&gl=IN&ceid=IN:en"
)


def search_google_news() -> List[Dict]:
    """Fetch Indian financial news from Google News RSS."""
    return _parse_rss(GOOGLE_NEWS_RSS, "Google News")


GOOGLE_NEWS_QUERIES = [
    "indian+stock+market+NSE+BSE+OR+markets",
    "RBI+OR+SEBI+OR+Indian+economy",
    "Sensex+OR+Nifty+OR+share+market+India",
    "Indian+stock+market+breaking+news",
    "NSE+corporate+results+OR+earnings+OR+dividend+India",
    "IPO+OR+buyback+OR+stock+split+OR+buyback+India+market",
    "FII+OR+DII+OR+FPI+flow+OR+foreign+investment+India",
    "crude+oil+OR+gold+OR+commodity+price+India+market",
    "India+IPO+OR+startup+funding+OR+venture+capital",
    "budget+OR+GDP+OR+inflation+OR+economic+data+India",
]


def search_google_news_multi() -> List[Dict]:
    """Search Google News with multiple focused queries."""
    articles = []
    for query in GOOGLE_NEWS_QUERIES:
        url = (
            "https://news.google.com/rss/search?"
            f"q={query}&hl=en-IN&gl=IN&ceid=IN:en"
        )
        result = _parse_rss(url, "Google News")
        articles.extend(result)
        time.sleep(0.5)

    seen_urls = set()
    unique = []
    for a in articles:
        url = a.get("url", "").rstrip("/")
        norm = url.lower().split("?")[0]
        if norm and norm not in seen_urls:
            seen_urls.add(norm)
            unique.append(a)

    log.info(f"Google News multi-search: {len(unique)} unique articles")
    return unique


# ──────────────────────────────────────────────────────────────────────
#  Web Search Engine (DuckDuckGo) — Fallback
# ──────────────────────────────────────────────────────────────────────

def search_duckduckgo(query: str) -> List[Dict]:
    """Search DuckDuckGo for a single query and return article dicts."""
    articles = []
    try:
        session = _get_session()
        resp = session.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query},
            timeout=15,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        for result in soup.select(".result, .web-result, .results_links"):
            title_el = result.select_one(".result__title a, .result__a, h2 a, a[data-testid='result-title-a']")
            snippet_el = result.select_one(".result__snippet, .snippet, .result__snippet__content")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            link = title_el.get("href", "")
            if not title or not link:
                continue
            # Extract actual URL from DuckDuckGo's redirect wrapper
            if "uddg=" in link:
                parsed = urlparse(link)
                qs = parse_qs(parsed.query)
                link = qs.get("uddg", [link])[0]
            # Skip DDG internal pages
            domain = urlparse(link).netloc.lower()
            if not link.startswith("http") or "duckduckgo.com" in domain:
                continue
            snippet = snippet_el.get_text(strip=True) if snippet_el else ""
            source = _infer_source(link)
            articles.append({
                "title": title,
                "url": link,
                "source": source,
                "snippet": snippet[:300],
            })
    except Exception as e:
        log.warning(f"DDG search failed: {e}")

    return articles


def search_breaking_news() -> List[Dict]:
    """
    Web search for breaking Indian market news via DDG.
    Used as fallback enrichment.
    """
    queries = [
        "site:economictimes.indiatimes.com stock market",
        "site:moneycontrol.com NSE BSE news",
        "site:livemint.com market",
        "site:businesstoday.in stock market",
        "site:business-standard.com markets",
        "breaking news Indian stock market today",
        "Indian share market top news today",
        "RBI budget SEBI news market impact India",
        "NSE corporate announcement board meeting outcome",
        "FII DII trading activity India stock market",
    ]

    all_articles = []
    for query in queries:
        results = search_duckduckgo(query)
        all_articles.extend(results)

    seen_urls = set()
    unique = []
    for a in all_articles:
        url = a.get("url", "").rstrip("/")
        norm = url.lower().split("?")[0].split("#")[0]
        if norm and norm not in seen_urls and len(a.get("title", "")) > 15:
            seen_urls.add(norm)
            unique.append(a)

    log.info(f"DuckDuckGo search: {len(unique)} unique articles from {len(queries)} queries")
    return unique


def _infer_source(url: str) -> str:
    """Infer news source name from URL domain."""
    domain = urlparse(url).netloc.lower()
    mapping = {
        "economictimes": "Economic Times",
        "moneycontrol": "Moneycontrol",
        "livemint": "Livemint",
        "business-standard": "Business Standard",
        "businessstandard": "Business Standard",
        "ndtvprofit": "NDTV Profit",
        "financialexpress": "Financial Express",
        "thehindubusinessline": "Hindu BusinessLine",
        "hindubusinessline": "Hindu BusinessLine",
        "zeebiz": "Zee Business",
        "businesstoday": "Business Today",
        "bloomberg": "Bloomberg",
        "reuters": "Reuters",
        "indiatimes": "India Times",
        "timesofindia": "Times of India",
        "indianexpress": "Indian Express",
        "thequint": "The Quint",
        "news18": "News18",
        "cnbc": "CNBC",
        "outlookindia": "Outlook India",
        "firstpost": "Firstpost",
        "theprint": "The Print",
        "inc42": "Inc42",
        "sebi": "SEBI",
        "investing": "Investing.com India",
    }
    for key, name in mapping.items():
        if key in domain:
            return name
    return "Web Search"


# ──────────────────────────────────────────────────────────────────────
#  Semantic Deduplication
# ──────────────────────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    """Normalize text for similarity comparison."""
    text = text.lower()
    text = re.sub(r'[^a-z0-9\s]', '', text)
    # Remove common stopwords
    stopwords = {'the', 'a', 'an', 'in', 'of', 'to', 'for', 'on', 'and', 'is',
                 'at', 'by', 'with', 'from', 'as', 'its', 'it', 'be', 'has',
                 'are', 'was', 'were', 'been', 'will', 'may', 'would', 'could',
                 'should', 'this', 'that', 'these', 'those', 'after', 'before',
                 'during', 'over', 'into', 'through', 'up', 'down', 'out',
                 'about', 'than', 'also', 'very', 'just', 'all', 'each',
                 'their', 'his', 'her', 'our', 'your', 'my', 'no', 'not',
                 'but', 'or', 'if', 'so', 'than', 'too', 'very', 'can', 'do'}
    words = [w for w in text.split() if w not in stopwords and len(w) > 2]
    return ' '.join(words)


def _similarity(a: str, b: str) -> float:
    """Jaccard similarity of two normalized strings."""
    words_a = set(_normalize(a).split())
    words_b = set(_normalize(b).split())
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)


# Source authority ranking (higher = more credible)
SOURCE_PRIORITY = {
    "NSE Filing": 10,
    "NSE Bulk Deal": 9,
    "NSE Block Deal": 9,
    "NSE Insider Trade": 9,
    "NSE SAST": 9,
    "NSE Pledge": 9,
    "NSE Rating Change": 8,
    "SEBI": 9,
    "SEBI Order": 8,
    "BSE Filing": 8,
    "BSE Insider Trade": 8,
    "PIB Release": 7,
    "Moneycontrol": 5,
    "Economic Times": 5,
    "Business Standard": 5,
    "Business Today": 5,
    "Hindu BusinessLine": 5,
    "Livemint": 4,
    "NDTV Profit": 4,
    "Financial Express": 4,
    "Google News": 3,
    "Inc42": 3,
    "Zee Business": 3,
    "Investing.com India": 2,
    "Web Search": 2,
}


def _deduplicate_semantic(articles: List[Dict], threshold: float = 0.45) -> List[Dict]:
    """
    Deduplicate articles by semantic similarity of their titles.
    When two articles are similar, keep the one with higher source priority
    or higher information content.
    """
    if not articles:
        return []

    scored = []
    for a in articles:
        title = a.get("title", "")
        source = a.get("source", "")
        snippet = a.get("snippet", "")
        # Calculate info content score (longer title + snippet = more info)
        info_score = len(title) + len(snippet) * 0.3
        source_score = SOURCE_PRIORITY.get(source, 3)
        a["_score"] = source_score * 2 + info_score * 0.01
        scored.append(a)

    # Sort by score descending (best quality first)
    scored.sort(key=lambda x: x["_score"], reverse=True)

    kept = []
    for article in scored:
        is_dup = False
        norm_title = _normalize(article.get("title", ""))
        for existing in kept:
            norm_existing = _normalize(existing.get("title", ""))
            if _similarity(norm_title, norm_existing) > threshold:
                is_dup = True
                break
        if not is_dup:
            # Clean up internal score field
            del article["_score"]
            kept.append(article)
            # Check if we have enough unique articles
            if len(kept) >= 100:
                break

    log.info(f"Semantic dedup: {len(articles)} -> {len(kept)} unique articles")
    return kept


# ──────────────────────────────────────────────────────────────────────
#  LLM News Analyzer
# ──────────────────────────────────────────────────────────────────────

def call_llm(messages: List[Dict], config: dict) -> Optional[str]:
    """Call the configured LLM and return the response text."""
    provider = config.get("llm_provider", "none")
    if provider == "none":
        log.info("LLM provider is 'none', skipping AI analysis")
        return None

    api_key = config.get("llm_api_key", "")
    if not api_key or "YOUR_" in api_key:
        log.warning("No valid LLM API key configured. Set it in config.py or via env var.")
        return None

    if provider == "anthropic":
        return _call_anthropic(messages, config)
    elif provider == "gemini":
        return _call_gemini(messages, config)
    else:
        return _call_openai_compatible(messages, config)


def _call_openai_compatible(messages: List[Dict], config: dict) -> Optional[str]:
    """Call any OpenAI-compatible API (OpenAI, Groq, DeepSeek, Together, etc.)."""
    try:
        resp = requests.post(
            config["llm_api_base"],
            headers={
                "Authorization": f"Bearer {config['llm_api_key']}",
                "Content-Type": "application/json",
            },
            json={
                "model": config["llm_model"],
                "messages": messages,
                "temperature": config.get("llm_temperature", 0.2),
                "max_tokens": config.get("llm_max_tokens", 2000),
            },
            timeout=90,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        log.error(f"OpenAI-compatible LLM call failed: {e}")
        if hasattr(e, "response") and e.response is not None:
            log.error(f"Response body: {e.response.text[:500]}")
        return None


def _call_anthropic(messages: List[Dict], config: dict) -> Optional[str]:
    """Call Anthropic Claude API."""
    try:
        system = ""
        anthropic_messages = []
        for m in messages:
            if m["role"] == "system":
                system = m["content"]
            else:
                anthropic_messages.append({"role": m["role"], "content": m["content"]})

        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": config["llm_api_key"],
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": config.get("llm_model", "claude-3-haiku-20240307"),
                "max_tokens": config.get("llm_max_tokens", 2000),
                "system": system,
                "messages": anthropic_messages,
                "temperature": config.get("llm_temperature", 0.2),
            },
            timeout=90,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["content"][0]["text"]
    except Exception as e:
        log.error(f"Anthropic LLM call failed: {e}")
        if hasattr(e, "response") and e.response is not None:
            log.error(f"Response body: {e.response.text[:500]}")
        return None


def _call_gemini(messages: List[Dict], config: dict) -> Optional[str]:
    """Call Google Gemini API (free tier via API key)."""
    try:
        api_key = config["llm_api_key"]
        model = config.get("llm_model", "gemini-2.0-flash")
        url = f"https://generativelanguage.googleapis.com/v1/models/{model}:generateContent"

        # Convert OpenAI-style messages to Gemini format
        # Gemini doesn't support systemInstruction on all models,
        # so we prepend system messages to the first user message
        system_text = ""
        contents = []
        for m in messages:
            role = m["role"]
            text = m["content"]
            if role == "system":
                system_text = text + "\n\n"
            elif role == "assistant":
                contents.append({"role": "model", "parts": [{"text": text}]})
            else:
                contents.append({"role": "user", "parts": [{"text": system_text + text}]})
                system_text = ""

        body = {
            "contents": contents,
            "generationConfig": {
                "temperature": config.get("llm_temperature", 0.2),
                "maxOutputTokens": config.get("llm_max_tokens", 2000),
            },
        }

        resp = requests.post(url, json=body, headers={"x-goog-api-key": api_key}, timeout=90)
        resp.raise_for_status()
        data = resp.json()

        candidates = data.get("candidates", [])
        if candidates and "content" in candidates[0]:
            parts = candidates[0]["content"].get("parts", [])
            if parts:
                return parts[0].get("text", "")
        log.error(f"Unexpected Gemini response: {json.dumps(data)[:300]}")
        return None
    except Exception as e:
        log.error(f"Gemini LLM call failed: {e}")
        if hasattr(e, "response") and e.response is not None:
            log.error(f"Response body: {e.response.text[:500]}")
        return None


ANALYSIS_PROMPT = """You are an expert Indian stock market analyst tracking NSE/BSE.

Your ONLY task: Select news that will DEFINITELY move Indian stocks or indices TODAY or TOMORROW.

Every article must have a "type" field:
- "stock": Company-specific catalyst that will move a PARTICULAR STOCK (earnings, order win, management change, regulatory action on company, buyback, insider/bulk deal)
- "index": Broad market news that will move INDICES/SECTORS (RBI policy, budget, GDP/inflation data, crude oil, FII flows, geopolitical, Fed rate, sector-wide news)
- If it names a specific company WITH a catalyst → "stock"
- If it's about the market/economy/sector without naming a specific company catalyst → "index"

RATING SCALE (market_impact_score 1-10):
- 10: Certain large-cap mover. Nifty 50 company: earnings surprise, blockbuster order win, buyback
- 8-9: Strong. RBI/budget/GST policy, mid-cap major order/regulatory, bulk/block deal
- 6-7: Moderate. Small-cap catalyst (order win), macro data (GDP/CPI), FII data
- 4-5: Low conviction. Market commentary without catalyst
- 1-3: Noise. Exclude.

NSE FILING RULES:
- Small-cap routine filing (Appointment of CS, Newspaper Publication) = max 5, type "stock"
- Small-cap order win/board outcome = score 6-7, type "stock"
- Any Nifty 50 company filing = score normally, type "stock"

Return ONLY raw JSON array. Objects: title, source, url, market_impact_score (integer 1-10), type ("stock" or "index"), reason (name stocks/catalyst).
Aim for about 60% "stock" and 40% "index" type articles in your selection.

Articles:{articles_json}
"""


def analyze_articles(articles: List[Dict], config: dict) -> List[Dict]:
    """Use LLM to analyze and rank articles by market impact."""
    if not articles:
        log.warning("No articles to analyze")
        return []

    top_n = config.get("top_news_count", 7)
    min_score = config.get("min_impact_score", 5)

    def _filter_by_min_score(articles_list):
        return [a for a in articles_list if a.get("market_impact_score", 0) >= min_score]

    if config.get("llm_provider") == "none" or not config.get("llm_api_key") or "YOUR_" in config.get("llm_api_key", ""):
        log.info("LLM not configured — using keyword-based fallback scoring")
        return _filter_by_min_score(_keyword_score(articles, top_n))

    # Two-pass approach: keyword pre-filter → LLM final ranking
    # Pass 1: Score all articles with keywords, keep top candidates for LLM exploration
    pre_scored = _keyword_score(articles, 60)
    pre_scored.sort(key=lambda x: x.get("market_impact_score", 0), reverse=True)
    candidates = pre_scored[:40]

    # Pass 2: Send only top candidates to LLM for intelligent ranking
    articles_for_prompt = [
        {"title": a["title"], "source": a["source"], "url": a.get("url", ""), "snippet": a.get("snippet", "")[:150]}
        for a in candidates
    ]

    prompt = ANALYSIS_PROMPT.format(
        articles_json=json.dumps(articles_for_prompt, ensure_ascii=False, indent=2),
    )

    messages = [
        {"role": "system", "content": "You are a precise JSON-only assistant. Never include markdown or explanations outside the JSON."},
        {"role": "user", "content": prompt},
    ]

    response = call_llm(messages, config)
    if not response:
        log.warning("LLM returned empty response — using keyword fallback")
        return _filter_by_min_score(_keyword_score(articles, top_n))

    ranked = _parse_llm_response(response)
    if not ranked:
        log.warning("Could not parse LLM response — using keyword fallback")
        return _filter_by_min_score(_keyword_score(articles, top_n))

    ranked = [a for a in ranked if a.get("market_impact_score", 0) >= min_score]
    ranked.sort(key=lambda x: x.get("market_impact_score", 0), reverse=True)
    log.info(f"LLM ranked {len(ranked)} articles (min score {min_score}+)")

    # Apply source diversity: 50-50 split NSE/Media for stock-type articles
    ranked = _diversify_sources(ranked, max_nse=6)
    return ranked[:top_n]


def _diversify_sources(articles: List[Dict], max_nse: int = 6) -> List[Dict]:
    """Ensure source diversity — 50-50 split between NSE filings and media."""
    nse_count = 0
    media_needed = max_nse  # aim for equal media representation
    media_count = 0
    diverse = []
    for a in articles:
        source = (a.get("source") or "").lower()
        is_nse = "nse" in source or "filing" in source
        a_type = a.get("type", "stock")

        if a_type == "index":
            # Index movers: always include (no cap)
            diverse.append(a)
        elif is_nse:
            if nse_count < max_nse:
                nse_count += 1
                diverse.append(a)
        else:
            if media_count < max_nse + 3:  # allow slightly more media
                media_count += 1
                diverse.append(a)
    log.info(f"Diversity filter: {len(articles)} -> {len(diverse)} ({nse_count} NSE, {media_count} media)")
    return diverse


def _parse_llm_response(response: str) -> List[Dict]:
    """Parse the LLM JSON response with multiple fallback strategies."""
    # Strategy 1: Find JSON array in response (greedy match)
    json_match = re.search(r"\[\s*\{.*\}\]", response, re.DOTALL)
    if json_match:
        try:
            result = json.loads(json_match.group())
            if isinstance(result, list) and len(result) > 0:
                return result
        except json.JSONDecodeError:
            pass

    # Strategy 2: Parse entire response as JSON
    try:
        return json.loads(response.strip())
    except json.JSONDecodeError:
        pass

    # Strategy 3: Extract from markdown code blocks
    code_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", response)
    if code_match:
        try:
            return json.loads(code_match.group(1).strip())
        except json.JSONDecodeError:
            pass

    log.error(f"Failed to parse LLM response as JSON: {response[:300]}")
    return []


def _keyword_score(articles: List[Dict], top_n: int = 10) -> List[Dict]:
    """Fallback scoring using keywords if LLM unavailable."""

    # Nifty 50 companies (any mention = potential stock mover)
    NIFTY50 = [
        "reliance", "tcs", "hdfc bank", "infosys", "hul", "icici bank",
        "hdfc", "itc", "sbi", "bharti airtel", "kotak mahindra", "l&t",
        "wipro", "axis bank", "bajaj finance", "asian paints", "maruti",
        "sun pharma", "tata motors", "ntpc", "m&m", "titan", "jsw steel",
        "powergrid", "nestle", "tech mahindra", "hcl tech", "bajaj finserv",
        "ultratech cement", "sbi life", "tata steel", "grasim", "cipla",
        "dabur", "hindalco", "ongc", "adani ports", "dr reddy", "eicher",
        "indusind bank", "bajaj auto", "divi lab", "britannia", "coal india",
        "bpcl", "shriram finance", "adani enterprises", "herouno moto",
        "hdfc life", "apollo hospitals", "adani green", "adani transmission",
        "wipro", "icici pruden", "sbi card", "dmart", "tata consumer",
        "havells", "torrent pharma", "marico", "berger paints",
    ]

    CATALYST_KEYWORDS = [
        # Order wins & business
        "order win", "order worth", "contract", "bag order", "award order",
        "new order", "export order", "secures order", "gets order",
        "letter of intent", "work order", "subscription",
        # Regulatory & govt
        "sebi order", "sebi notice", "sebi ban", "sebi fine",
        "income tax raid", "ed raid", "ed summons", "investigation",
        "show cause", "penalty", "regulatory approval", "government approval",
        "dpiit", "nclt", "bankruptcy", "resolution plan",
        "sebi approval", "sebi clearance",
        # Earnings & corporate actions
        "quarterly results", "q1 results", "q2 results", "q3 results", "q4 results",
        "profit rises", "profit falls", "revenue growth", "net profit",
        "ebitda", "margin expansion", "earnings beat", "earnings miss",
        "buyback", "dividend declared", "stock split", "bonus issue",
        "rights issue", "delisting", "corporate action",
        "profit after tax", "operating revenue", "net profit jumps",
        # M&A
        "acquisition", "merger", "demerger", "stake sale", "stake buy",
        "promoter sells", "promoter buys", "bulk deal", "block deal",
        "fii buy", "dii buy", "anchor investor",
        "open offer", "takeover",
        # Management
        "ceo resign", "cfo resign", "appointment", "management change",
        "board meeting", "board approves", "managing director",
        "key managerial",
        # Market moving
        "rbi policy", "repo rate", "crr", "slr", "monetary policy",
        "union budget", "fiscal deficit", "gst council",
        "ipo listing", "listing gain", "listing loss", "ipo opens", "ipo closes",
        "iip data", "cpi inflation", "gdp data", "pmi data",
        "fpi outflow", "fii outflow", "dii inflow",
        "crude oil price", "rupee fall", "rupee rise", "dollar index",
        "fed rate", "us fed", "interest rate cut",
        # Price sensitive
        "upper circuit", "lower circuit", "52-week high", "52-week low",
        "price target", "upgrade", "downgrade", "buy rating", "overweight",
        "target price", "accumulate", "add rating", "reduce rating",
        # Sector specific
        "gst collection", "tax revenue", "direct tax", "indirect tax",
        "core sector", "industrial production", "manufacturing pmi",
        "services pmi", "trade deficit", "current account",
        # Fund flow
        "mutual fund inflow", "sip inflow", "aum", "assets under management",
        "nfo", "new fund offer", "index rejig", "index rebalancing",
        "morgan stanley", "goldman sachs", "nomura", "clsa", "jpmorgan",
        # Startup & funding
        "funding round", "series a", "series b", "series c", "series d",
        "seed round", "pre-ipo", "valuation",
        "unicorn", "startup funding",
        # NSE specific
        "board meeting outcome", "corporate announcement",
        "trading window", "closure of trading window",
    ]

    scored = []
    for article in articles:
        text = f"{article['title']} {article.get('snippet', '')}".lower()
        source = article.get("source", "").lower()

        score = 1
        matched_keywords = []

        # Check for Nifty 50 company names (high weight)
        for company in NIFTY50:
            if company in text:
                score += 2
                matched_keywords.append(company)

        # Check for catalyst keywords
        for kw in CATALYST_KEYWORDS:
            if kw in text:
                score += 1
                matched_keywords.append(kw)

        # Source bonus: NSE filings are most authentic
        if "nse filing" in source:
            score += 2
        elif source == "google news":
            score += 0  # neutral

        # Cap and store
        score = min(score, 10)
        scored.append({
            "title": article["title"],
            "source": article["source"],
            "url": article.get("url", ""),
            "market_impact_score": score,
            "reason": _keyword_reason(text, score, matched_keywords),
        })

    scored.sort(key=lambda x: x["market_impact_score"], reverse=True)
    return scored[:top_n]


def _keyword_reason(text: str, score: int, matched_kws: list = None) -> str:
    if score >= 8:
        return "Direct stock-specific catalyst detected — high probability of market movement."
    elif score >= 6:
        return "Significant financial/regulatory trigger identified — moderate-to-high impact expected."
    elif score >= 4:
        return "Relevant market news with potential sector-level impact."
    else:
        return "General finance news — limited direct stock impact."


# ──────────────────────────────────────────────────────────────────────
#  Insider News Analysis (Separate from General News)
# ──────────────────────────────────────────────────────────────────────

INSIDER_ANALYSIS_PROMPT = """You are an expert Indian stock market analyst specializing in exchange filings and insider signals.

Your ONLY task: Analyze these exchange filing / insider trading signals and rank them by HOW LIKELY they are to move stock prices today.

Every article must have a "type" field:
- "insider_buy": Promoter/DII insider buying — bullish signal
- "insider_sell": Promoter/DII insider selling — bearish signal
- "bulk_deal": Large institutional bulk/block deal — shows smart money movement
- "pledge": Promoter pledge creation or release — distress or confidence
- "regulatory": SEBI order, regulatory action — can be strongly positive or negative
- "policy": Government/PIB announcement — sector or economy-wide impact
- "rating": Credit rating upgrade/downgrade — directly affects bond/stock pricing
- "filing": General corporate filing (announcement, board outcome, order win)

RATING SCALE (market_impact_score 1-10):
- 10: Major Nifty50 insider buy/bulk deal, SEBI ban on large company, rating upgrade of large cap
- 8-9: Significant insider trade (₹10Cr+), block deal, rating downgrade, SEBI investigation on mid-cap
- 6-7: Moderate insider trade, bulk deal, pledge release, small-cap rating change
- 4-5: Minor insider trade, routine filing, minor SEBI order
- 1-3: Noise. Exclude.

CRITICAL: These are EARLY SIGNALS from exchange filings. They have higher information value because they haven't been reported by media yet. Score them generously — a legitimate insider buy is usually a 7+ even for small-caps.

Return ONLY raw JSON array. Objects: title, source, url, market_impact_score (integer 1-10), type, reason (name stocks and what the signal means).

Articles:{articles_json}
"""


def analyze_insider_articles(articles: List[Dict], config: dict) -> List[Dict]:
    """Use LLM to analyze and rank insider/signal articles by market impact."""
    if not articles:
        log.warning("No insider articles to analyze")
        return []

    top_n = config.get("insider_top_news_count", 10)
    min_score = 5  # slightly lower threshold for insider signals

    def _filter_by_min_score(articles_list):
        return [a for a in articles_list if a.get("market_impact_score", 0) >= min_score]

    if config.get("llm_provider") == "none" or not config.get("llm_api_key") or "YOUR_" in config.get("llm_api_key", ""):
        log.info("LLM not configured — using keyword-based fallback for insider articles")
        return _filter_by_min_score(_keyword_score(articles, top_n))

    # Pre-filter: keyword score all articles, send top candidates to LLM for exploration
    pre_scored = _keyword_score(articles, 40)
    candidates = pre_scored[:30]

    articles_for_prompt = [
        {"title": a["title"], "source": a["source"], "url": a.get("url", ""), "snippet": a.get("snippet", "")[:150]}
        for a in candidates
    ]

    prompt = INSIDER_ANALYSIS_PROMPT.format(
        articles_json=json.dumps(articles_for_prompt, ensure_ascii=False, indent=2),
    )

    messages = [
        {"role": "system", "content": "You are a precise JSON-only assistant. Never include markdown or explanations outside the JSON."},
        {"role": "user", "content": prompt},
    ]

    response = call_llm(messages, config)
    if not response:
        log.warning("LLM returned empty response for insider — using keyword fallback")
        return _filter_by_min_score(_keyword_score(articles, top_n))

    ranked = _parse_llm_response(response)
    if not ranked:
        log.warning("Could not parse LLM response for insider — using keyword fallback")
        return _filter_by_min_score(_keyword_score(articles, top_n))

    ranked = [a for a in ranked if a.get("market_impact_score", 0) >= min_score]
    ranked.sort(key=lambda x: x.get("market_impact_score", 0), reverse=True)
    log.info(f"Insider LLM ranked {len(ranked)} articles (min score {min_score}+)")

    return ranked[:top_n]


# ──────────────────────────────────────────────────────────────────────
#  Telegram Notifier
# ──────────────────────────────────────────────────────────────────────

def send_telegram(articles: List[Dict], time_label: str, config: dict,
                  insider_articles: Optional[List[Dict]] = None) -> bool:
    """Send ranked news articles to Telegram with optional Insider News section."""
    token = config.get("telegram_bot_token", "")
    chat_id = config.get("telegram_chat_id", "")
    if not token or "YOUR_" in token or not chat_id or "YOUR_" in chat_id:
        log.warning("Telegram not configured. Set bot token and chat ID in config.py")
        return False

    formatted = _format_message(articles, time_label, config, insider_articles=insider_articles)
    if not formatted:
        log.warning("No news to send")
        return False

    chunks = _split_message(formatted)

    success = True
    for i, chunk in enumerate(chunks):
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=30,
            )
            resp.raise_for_status()
            log.info(f"Telegram message {i+1}/{len(chunks)} sent successfully")
        except Exception as e:
            log.error(f"Telegram send failed: {e}")
            success = False

    return success


def _format_message(articles: List[Dict], time_label: str, config: dict,
                    insider_articles: Optional[List[Dict]] = None) -> str:
    """Format articles into a readable HTML message with INSIDER + GENERAL sections."""
    if not articles and not insider_articles:
        return ""

    today = config.get("today", date.today().strftime("%B %d, %Y"))
    lines = []
    lines.append(f"<b>INDIAN MARKET NEWS — {time_label} | {today}</b>")
    lines.append("=" * 40)
    lines.append("")

    # ── INSIDER NEWS SECTION (Early signals from exchange filings) ──
    if insider_articles:
        lines.append("<b>🔴 INSIDER NEWS — Early Signals</b>")
        lines.append("<i>Exchange filings & regulatory data — unreported by mainstream media</i>")
        lines.append("")
        for i, article in enumerate(insider_articles, 1):
            title = article.get("title", "Untitled")
            source = article.get("source", "Unknown")
            url = article.get("url", "")
            score = article.get("market_impact_score", "?")
            reason = article.get("reason", "")

            bars = "|" * (min(score, 10) if isinstance(score, int) else 0)
            lines.append(f"<b>{i}. {title}</b>")
            lines.append(f"   Source: {source}")
            if url:
                lines.append(f"   <a href='{url}'>Read filing</a>")
            lines.append(f"   Impact: {bars} ({score}/10)")
            if reason:
                lines.append(f"   Why: {reason}")
            lines.append("")

    # ── GENERAL NEWS SECTION (Mainstream media) ──
    if articles:
        if insider_articles:
            lines.append("=" * 40)
            lines.append("")

        lines.append("<b>📰 GENERAL NEWS — Media Reports</b>")
        lines.append("")

        # Split into stock-specific and index-movers
        stock_items = [a for a in articles if a.get("type") == "stock"]
        index_items = [a for a in articles if a.get("type") != "stock"]

        if stock_items:
            lines.append("<b>--- STOCK-SPECIFIC CATALYSTS ---</b>")
            lines.append("")
            for i, article in enumerate(stock_items, 1):
                title = article.get("title", "Untitled")
                source = article.get("source", "Unknown")
                url = article.get("url", "")
                score = article.get("market_impact_score", "?")
                reason = article.get("reason", "")

                bars = "|" * (min(score, 10) if isinstance(score, int) else 0)
                lines.append(f"<b>{i}. {title}</b>")
                lines.append(f"   Source: {source}")
                if url:
                    lines.append(f"   <a href='{url}'>Read full article</a>")
                lines.append(f"   Impact: {bars} ({score}/10)")
                if reason:
                    lines.append(f"   Why: {reason}")
                lines.append("")

        if index_items:
            lines.append("<b>--- INDEX MOVERS & BREAKING NEWS ---</b>")
            lines.append("")
            for i, article in enumerate(index_items, len(stock_items) + 1):
                title = article.get("title", "Untitled")
                source = article.get("source", "Unknown")
                url = article.get("url", "")
                score = article.get("market_impact_score", "?")
                reason = article.get("reason", "")

                bars = "|" * (min(score, 10) if isinstance(score, int) else 0)
                lines.append(f"<b>{i}. {title}</b>")
                lines.append(f"   Source: {source}")
                if url:
                    lines.append(f"   <a href='{url}'>Read full article</a>")
                lines.append(f"   Impact: {bars} ({score}/10)")
                if reason:
                    lines.append(f"   Why: {reason}")
                lines.append("")

    lines.append("=" * 40)
    lines.append("Market Intel Agent | Next update in ~5-7 hours")

    return "\n".join(lines)


def _split_message(text: str, max_length: int = 3800) -> List[str]:
    """Split a long message into chunks at paragraph boundaries."""
    if len(text) <= max_length:
        return [text]

    chunks = []
    paragraphs = text.split("\n\n")
    current = ""

    for para in paragraphs:
        if len(current) + len(para) + 2 > max_length:
            if current:
                chunks.append(current.strip())
            current = para + "\n\n"
        else:
            current += para + "\n\n"

    if current.strip():
        chunks.append(current.strip())

    return chunks


# ──────────────────────────────────────────────────────────────────────
#  Main Orchestrator
# ──────────────────────────────────────────────────────────────────────

def collect_news(config: dict) -> List[Dict]:
    """Collect news from all sources, prioritizing early-signal data."""
    all_articles = []
    sources_enabled = config.get("news_sources", {})

    # Tier 0: NSE Corporate Announcements (earliest signals — exchange filings)
    try:
        nse_articles = scrape_nse_high_value()
        all_articles.extend(nse_articles)
        time.sleep(config.get("request_delay", 1.0))
    except Exception as e:
        log.error(f"NSE announcements failed: {e}")

    # Tier 1: Google News RSS (broad coverage)
    try:
        google_articles = search_google_news_multi()
        all_articles.extend(google_articles)
        time.sleep(config.get("request_delay", 1.0))
    except Exception as e:
        log.error(f"Google News search failed: {e}")

    # Tier 2: Source-specific scrapers (RSS primary, HTML fallback)
    scraper_map = {
        "moneycontrol": scrape_moneycontrol,
        "economictimes": scrape_economictimes,
        "business_standard": scrape_business_standard,
        "livemint": scrape_livemint,
        "ndtv_profit": scrape_ndtv_profit,
        "financial_express": scrape_financial_express,
        "the_hindu_businessline": scrape_the_hindu_businessline,
        "zeebiz": scrape_zeebiz,
        "business_today": scrape_business_today,
        "inc42": scrape_inc42,
        "sebi": scrape_sebi,
        "investing_india": scrape_investing_india,
    }

    for source_name, scraper_fn in scraper_map.items():
        if sources_enabled.get(source_name, True):
            try:
                articles = scraper_fn()
                all_articles.extend(articles)
                time.sleep(config.get("request_delay", 1.0))
            except Exception as e:
                log.error(f"Scraper '{source_name}' failed: {e}")

    # Tier 3: DuckDuckGo web search for enrichment
    try:
        web_articles = search_breaking_news()
        all_articles.extend(web_articles)
    except Exception as e:
        log.error(f"Web search enrichment failed: {e}")

    # Deduplicate by URL (exact)
    seen_urls = set()
    url_deduped = []
    for a in all_articles:
        url = a.get("url", "").rstrip("/")
        norm = url.lower().split("?")[0].split("#")[0]
        if norm and norm not in seen_urls and len(a.get("title", "")) > 10:
            seen_urls.add(norm)
            url_deduped.append(a)

    log.info(f"After URL dedup: {len(url_deduped)} articles")

    # Deduplicate by semantic similarity (catch same story from different sources)
    deduped = _deduplicate_semantic(url_deduped, threshold=0.30)

    log.info(f"After semantic dedup: {len(deduped)} unique articles")
    return deduped


def get_time_label(config: dict) -> str:
    """Determine the schedule label based on IST (UTC+5:30)."""
    ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    hour = ist_now.hour
    if hour < 11:
        return config.get("schedule_label", {}).get("0900", "9:00 AM")
    elif hour < 17:
        return config.get("schedule_label", {}).get("1400", "2:00 PM")
    else:
        return config.get("schedule_label", {}).get("1900", "7:00 PM")


def main():
    """Main entry point for the agent — collects & sends both Insider and General News."""
    log.info("=" * 60)
    log.info("Indian Market News Agent — Starting run")

    config = load_config()
    time_label = get_time_label(config)
    log.info(f"Schedule slot: {time_label}")

    # ── Step 1A: Collect insider news (early signals from exchange filings) ──
    log.info("Step 1A: Collecting insider / early-signal news from exchange filings...")
    insider_articles = collect_insider_news(config)
    insider_ranked = []
    if insider_articles:
        log.info(f"Step 2A: Analyzing {len(insider_articles)} insider articles...")
        insider_ranked = analyze_insider_articles(insider_articles, config)
        log.info(f"Insider analysis: {len(insider_ranked)} ranked articles")
    else:
        log.info("No insider articles collected")

    # ── Step 1B: Collect general news (mainstream media) ──
    log.info("Step 1B: Collecting general news from sources...")
    articles = collect_news(config)

    ranked = []
    if not articles:
        log.warning("No general news articles collected from any source")
    else:
        log.info(f"Step 2B: Analyzing {len(articles)} general articles for market impact...")
        ranked = analyze_articles(articles, config)

    if not ranked and not insider_ranked:
        log.warning("No articles passed impact threshold")
        print("NO_IMPACTFUL_NEWS")
        return

    # ── Step 3: Send to Telegram (both sections in one message) ──
    total = len(ranked) + len(insider_ranked)
    log.info(f"Step 3: Sending {total} articles to Telegram ({len(insider_ranked)} insider + {len(ranked)} general)...")
    sent = send_telegram(ranked, time_label, config, insider_articles=insider_ranked)

    # Console output
    print(f"\n{'='*60}")
    print(f"MARKET NEWS — {time_label}")
    print(f"{'='*60}")

    if insider_ranked:
        print(f"\n--- INSIDER NEWS ({len(insider_ranked)} items) ---")
        for i, a in enumerate(insider_ranked, 1):
            print(f"{i}. [{a.get('market_impact_score', '?')}/10] {a['title']}")
            print(f"   Source: {a['source']} | {a.get('reason', '')}")
            print(f"   URL: {a.get('url', '')}")

    if ranked:
        print(f"\n--- GENERAL NEWS ({len(ranked)} items) ---")
        for i, a in enumerate(ranked, 1):
            print(f"{i}. [{a.get('market_impact_score', '?')}/10] {a['title']}")
            print(f"   Source: {a['source']} | {a.get('reason', '')}")
            print(f"   URL: {a.get('url', '')}")

    print(f"{'='*60}")
    print(f"Telegram sent: {sent}")
    print(f"{'='*60}\n")

    log.info("Agent run completed successfully")
    return {"general": ranked, "insider": insider_ranked}


if __name__ == "__main__":
    main()
