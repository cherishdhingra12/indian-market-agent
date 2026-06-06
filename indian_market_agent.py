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
from datetime import datetime, date
from typing import List, Dict, Optional, Tuple
from urllib.parse import urljoin, urlparse, parse_qs
from html import unescape

import requests
from bs4 import BeautifulSoup

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
    """Scrape NDTV Profit via RSS + HTML."""
    articles = _parse_rss("https://www.ndtvprofit.com/rss/latest", "NDTV Profit")
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
    """Scrape Financial Express via RSS + HTML."""
    articles = _parse_rss("https://www.financialexpress.com/feed/", "Financial Express")
    if len(articles) >= 10:
        return articles

    soup = _fetch_soup("https://www.financialexpress.com/market/")
    if not soup:
        return articles

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
    """Scrape Zee Business via RSS."""
    return _parse_rss("https://www.zeebiz.com/rss/market.xml", "Zee Business")


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
        "breaking news Indian stock market today",
        "Indian share market top news today",
        "RBI budget SEBI news market impact India",
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
        "bloomberg": "Bloomberg",
        "reuters": "Reuters",
        "indiatimes": "India Times",
        "timesofindia": "Times of India",
        "indianexpress": "Indian Express",
        "thequint": "The Quint",
        "news18": "News18",
        "cnbc": "CNBC",
        "businesstoday": "Business Today",
        "outlookindia": "Outlook India",
        "firstpost": "Firstpost",
        "theprint": "The Print",
    }
    for key, name in mapping.items():
        if key in domain:
            return name
    return "Web Search"


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
        url = f"https://generativelanguage.googleapis.com/v1/models/{model}:generateContent?key={api_key}"

        # Convert OpenAI-style messages to Gemini format
        system_instruction = None
        contents = []
        for m in messages:
            role = m["role"]
            text = m["content"]
            if role == "system":
                system_instruction = {"parts": [{"text": text}]}
            elif role == "assistant":
                contents.append({"role": "model", "parts": [{"text": text}]})
            else:
                contents.append({"role": "user", "parts": [{"text": text}]})

        body = {
            "contents": contents,
            "generationConfig": {
                "temperature": config.get("llm_temperature", 0.2),
                "maxOutputTokens": config.get("llm_max_tokens", 2000),
            },
        }
        if system_instruction:
            body["systemInstruction"] = system_instruction

        resp = requests.post(url, json=body, timeout=90)
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


ANALYSIS_PROMPT = """You are an expert Indian stock market analyst with 20 years of experience tracking NSE and BSE.

Your task: Given the following list of news articles collected from Indian financial sources, identify the TOP {top_n} articles that would SUBSTANTIALLY affect the movement of the Indian stock market (NSE/BSE) TODAY or TOMORROW.

Evaluation criteria (score 1-10 for each article):
- 9-10: Direct impact on market indices (RBI policy, budget, geopolitical events, major corporate earnings of index heavyweights)
- 7-8: Strong sector-level impact (major deals, regulatory changes, key economic data like GDP/IIP/CPI)
- 5-6: Moderate impact (company-specific news with large market cap, sector trends)
- 3-4: Low impact (routine announcements, minor corporate news)
- 1-2: Negligible impact (general business news, personal finance tips)

Return ONLY a valid JSON array (no markdown, no code blocks, no extra text) with objects containing these EXACT fields:
- "title": the article headline
- "source": the source name
- "url": the article URL
- "market_impact_score": integer from 1 to 10
- "reason": one sentence explaining the market impact specifically in Indian market context

Articles:{articles_json}
"""


def analyze_articles(articles: List[Dict], config: dict) -> List[Dict]:
    """Use LLM to analyze and rank articles by market impact."""
    if not articles:
        log.warning("No articles to analyze")
        return []

    top_n = config.get("top_news_count", 7)

    if config.get("llm_provider") == "none" or not config.get("llm_api_key") or "YOUR_" in config.get("llm_api_key", ""):
        log.info("LLM not configured — using keyword-based fallback scoring")
        return _keyword_score(articles, top_n)

    articles_for_prompt = [
        {"title": a["title"], "source": a["source"], "url": a.get("url", ""), "snippet": a.get("snippet", "")[:200]}
        for a in articles
    ]

    prompt = ANALYSIS_PROMPT.format(
        top_n=top_n,
        articles_json=json.dumps(articles_for_prompt, ensure_ascii=False, indent=2),
    )

    messages = [
        {"role": "system", "content": "You are a precise JSON-only assistant. Never include markdown or explanations outside the JSON."},
        {"role": "user", "content": prompt},
    ]

    response = call_llm(messages, config)
    if not response:
        log.warning("LLM returned empty response — using keyword fallback")
        return _keyword_score(articles, top_n)

    ranked = _parse_llm_response(response)
    if not ranked:
        log.warning("Could not parse LLM response — using keyword fallback")
        return _keyword_score(articles, top_n)

    min_score = config.get("min_impact_score", 5)
    ranked = [a for a in ranked if a.get("market_impact_score", 0) >= min_score]
    ranked.sort(key=lambda x: x.get("market_impact_score", 0), reverse=True)
    log.info(f"LLM ranked {len(ranked)} articles (min score {min_score}+)")
    return ranked[:top_n]


def _parse_llm_response(response: str) -> List[Dict]:
    """Parse the LLM JSON response with multiple fallback strategies."""
    # Strategy 1: Find JSON array in response
    json_match = re.search(r"\[\s*\{.*?\}\s*\]", response, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group())
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
    HIGH_IMPACT_KEYWORDS = [
        "rbi", "repo rate", "monetary policy", "budget", "fiscal deficit",
        "gdp", "inflation", "cpi", "iip", "manufacturing pmi", "services pmi",
        "geopolitical", "war", "sanction", "crude oil", "gst council",
        "sebi", "fdi", "fpi", "fii", "dii", "foreign institutional",
        "excise duty", "income tax", "corporate tax",
        "ipo", "listing", "results", "quarterly earnings",
        "merger", "acquisition", "takeover", "buyback", "dividend",
        "rating", "credit rating", "moody", "s&p", "fitch",
        "banking", "hdfc", "icici", "sbi", "reliance", "tata", "infosys",
        "nifty", "sensex", "bse", "nse", "benchmark", "index",
        "block deal", "bulk deal", "anchor", "ipo listing",
        "trade war", "tariff", "export", "import", "current account",
        "forex", "rupee", "dollar", "fed rate", "us fed",
        "adani", "ambani", "tata group",
    ]

    scored = []
    for article in articles:
        text = f"{article['title']} {article.get('snippet', '')}".lower()
        score = 1
        for kw in HIGH_IMPACT_KEYWORDS:
            if kw.lower() in text:
                score += 1
        score = min(score, 8)
        scored.append({
            "title": article["title"],
            "source": article["source"],
            "url": article.get("url", ""),
            "market_impact_score": score,
            "reason": _keyword_reason(text, score),
        })

    scored.sort(key=lambda x: x["market_impact_score"], reverse=True)
    return scored[:top_n]


def _keyword_reason(text: str, score: int) -> str:
    if score >= 7:
        return "Contains multiple high-impact financial keywords — likely to move the market."
    elif score >= 5:
        return "Contains significant financial/economic terms — moderate market impact expected."
    elif score >= 3:
        return "Contains some relevant financial terms — potential sector-level impact."
    else:
        return "General financial news — limited direct market impact."


# ──────────────────────────────────────────────────────────────────────
#  Telegram Notifier
# ──────────────────────────────────────────────────────────────────────

def send_telegram(articles: List[Dict], time_label: str, config: dict) -> bool:
    """Send ranked news articles to Telegram."""
    token = config.get("telegram_bot_token", "")
    chat_id = config.get("telegram_chat_id", "")
    if not token or "YOUR_" in token or not chat_id or "YOUR_" in chat_id:
        log.warning("Telegram not configured. Set bot token and chat ID in config.py")
        return False

    formatted = _format_message(articles, time_label, config)
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


def _format_message(articles: List[Dict], time_label: str, config: dict) -> str:
    """Format articles into a readable HTML message (no emojis as per user preference)."""
    if not articles:
        return ""

    today = config.get("today", date.today().strftime("%B %d, %Y"))
    lines = []
    lines.append(f"<b>INDIAN MARKET NEWS — {time_label} | {today}</b>")
    lines.append("=" * 40)
    lines.append("")

    for i, article in enumerate(articles, 1):
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
    """Collect news from all enabled sources (RSS + HTML + Web Search)."""
    all_articles = []
    sources_enabled = config.get("news_sources", {})

    # Tier 1: Google News RSS (most reliable, broad coverage)
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

    # Deduplicate by URL (normalized)
    seen_urls = set()
    unique = []
    for a in all_articles:
        url = a.get("url", "").rstrip("/")
        norm = url.lower().split("?")[0].split("#")[0]
        if norm and norm not in seen_urls and len(a.get("title", "")) > 10:
            seen_urls.add(norm)
            unique.append(a)

    log.info(f"Total unique articles collected: {len(unique)}")
    return unique


def get_time_label(config: dict) -> str:
    """Determine the schedule label based on current hour."""
    hour = datetime.now().hour
    if hour < 11:
        return config.get("schedule_label", {}).get("0900", "9:00 AM")
    elif hour < 17:
        return config.get("schedule_label", {}).get("1400", "2:00 PM")
    else:
        return config.get("schedule_label", {}).get("1900", "7:00 PM")


def main():
    """Main entry point for the agent."""
    log.info("=" * 60)
    log.info("Indian Market News Agent — Starting run")

    config = load_config()
    time_label = get_time_label(config)
    log.info(f"Schedule slot: {time_label}")

    # Step 1: Collect news from all sources
    log.info("Step 1: Collecting news from sources...")
    articles = collect_news(config)

    if not articles:
        log.warning("No articles collected from any source")
        print("NO_NEWS_COLLECTED")
        return

    # Step 2: Analyze and rank by market impact
    log.info(f"Step 2: Analyzing {len(articles)} articles for market impact...")
    ranked = analyze_articles(articles, config)

    if not ranked:
        log.warning("No articles passed impact threshold")
        print("NO_IMPACTFUL_NEWS")
        return

    # Step 3: Send to Telegram
    log.info(f"Step 3: Sending top {len(ranked)} articles to Telegram...")
    sent = send_telegram(ranked, time_label, config)

    # Console output
    print(f"\n{'='*60}")
    print(f"TOP {len(ranked)} MARKET-MOVING NEWS — {time_label}")
    print(f"{'='*60}")
    for i, a in enumerate(ranked, 1):
        print(f"{i}. [{a.get('market_impact_score', '?')}/10] {a['title']}")
        print(f"   Source: {a['source']} | Impact: {a.get('reason', '')}")
        print(f"   URL: {a.get('url', '')}")
    print(f"{'='*60}")
    print(f"Telegram sent: {sent}")
    print(f"{'='*60}\n")

    log.info("Agent run completed successfully")
    return ranked


if __name__ == "__main__":
    main()
