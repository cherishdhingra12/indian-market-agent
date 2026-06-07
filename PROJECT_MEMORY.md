# PROJECT MEMORY — Indian Market News Agent

## 1. PROJECT OVERVIEW

An automated **Indian Financial Market News Agent** that scrapes financial news from 17+ sources, ranks them by stock market impact using Google Gemini AI, and delivers the top stories to Telegram 3x daily (9 AM, 2 PM, 7 PM IST).

**Purpose:** Give retail traders early access to market-moving news before it hits mainstream — NSE corporate filings (real-time exchange data) combined with media coverage, analyzed and ranked by AI.

**Deployment:** GitHub Actions (free cloud, 3x daily UTC cron mapped to IST).

**GitHub Repo:** `https://github.com/cherishdhingra12/indian-market-agent.git` (auth via GitHub PAT / token)

---

## 2. ARCHITECTURE & DATA FLOW

```
NSE Corporate Filings ─┐
Google News RSS ───────┤
Moneycontrol RSS/HTML ─┤
Economic Times ────────┤
Business Standard ─────┤
Livemint ──────────────┤──▶ col─lect_news() ──▶ URL dedup ──▶ Semantic dedup ──▶ analyze_articles() ──▶ Telegram
NDTV Profit ───────────┤                                (60-1300 raw)   (100 unique)
Financial Express ─────┤
Hindu BusinessLine ────┤                                      │
Zee Business ──────────┤                              Keyword pre-filter
Business Today ────────┤                              (top 60 → top 40)
Inc42 ─────────────────┤                                      │
SEBI ──────────────────┤                              Gemini 2.5 Flash Lite
Investing.com India ───┤                              (final ranking, top 7-15)
DuckDuckGo Web ────────┘                              Diversity filter (50-50 NSE/media)
```

**Pipeline (3 steps in main()):**
1. `collect_news()` — Tier 0: NSE → Tier 1: Google News → Tier 2: Site scrapers → Tier 3: DuckDuckGo
2. `analyze_articles()` — Keyword pre-filter → Gemini LLM → Source diversity filter
3. `send_telegram()` — Format as two-section HTML message → Send via Telegram Bot API

---

## 3. FILE STRUCTURE

| File | Lines | Purpose |
|------|-------|---------|
| `indian_market_agent.py` | 1503 | Main agent: all scraping, analysis, LLM, Telegram logic |
| `config.py` | 48 | Configuration constants (LLM, Telegram, sources, thresholds) |
| `.env` | N/A | API keys (gitignored, loaded by run_agent.sh) |
| `.env.example` | 17 | Template for .env |
| `requirements.txt` | 3 | `requests>=2.31.0`, `beautifulsoup4>=4.12.0`, `lxml>=5.0.0` |
| `run_agent.sh` | 37 | Shell wrapper: sources .env, calls agent, logs output |
| `setup_cron.sh` | 81 | Setup script for local cron or GitHub Actions |
| `.github/workflows/market_agent.yml` | 39 | GitHub Actions workflow (3x daily cron) |
| `.gitignore` | 6 | Ignores .env, __pycache__, logs/, *.pyc, .DS_Store, venv/ |
| `logs/agent.log` | ~705 | Persistent log of all runs |
| `logs/run_*.log` | Varies | Per-run log files from run_agent.sh |
| `PROJECT_MEMORY.md` | This file | Complete project memory for AI/agent context recall |

---

## 4. CONFIGURATION (config.py)

### LLM Settings
- `LLM_PROVIDER = "gemini"` — Options: "gemini", "groq", "openai", "anthropic", "none"
- `LLM_API_KEY` — From env var `GEMINI_API_KEY` (free key from aistudio.google.com)
- `LLM_MODEL = "gemini-2.5-flash-lite"` — Free tier, 60 req/min, 1500 req/day
- `LLM_TEMPERATURE = 0.2`
- `LLM_MAX_TOKENS = 8192`

### Telegram
- `TELEGRAM_BOT_TOKEN` — From env var, created via @BotFather
- `TELEGRAM_CHAT_ID` — From env var, get via @userinfobot

### Agent Behaviour
- `TOP_NEWS_COUNT = 15` — Max articles in final Telegram output
- `MIN_IMPACT_SCORE = 6` — Minimum impact score threshold
- `REQUEST_DELAY = 1.2` — Seconds between API requests
- `SCHEDULE_LABEL` — Display labels for 3 time slots

### News Sources (all enabled by default)
`nse_announcements`, `moneycontrol`, `economictimes`, `business_standard`, `livemint`, `ndtv_profit`, `financial_express`, `the_hindu_businessline`, `zeebiz`, `business_today`, `inc42`, `sebi`, `investing_india`

---

## 5. COMPLETE FUNCTION MAP (indian_market_agent.py)

### Configuration & Startup
| Line | Function | Purpose |
|------|----------|---------|
| 50 | `load_config()` | Load config from config.py module with fallback to env vars |
| 76 | `_default_config()` | Fallback defaults when config.py unavailable |

### HTTP Utilities
| Line | Function | Purpose |
|------|----------|---------|
| 100 | `USER_AGENTS` | 4 rotating User-Agent strings for anti-blocking |
| 109 | `_get_session()` | Create requests.Session with rotating UA, headers, delay |
| 130 | `_fetch_soup()` | Fetch URL → BeautifulSoup (lxml parser) |
| 142 | `_fetch_text()` | Fetch URL → raw text |
| 154 | `_fetch_bytes()` | Fetch URL → raw bytes (preserves encoding) |

### RSS Feed Parser
| Line | Function | Purpose |
|------|----------|---------|
| 170 | `_parse_rss()` | Parse RSS 2.0 and Atom feeds → List[Dict] |
| 215 | `_rss_text()` | Extract text from RSS XML element |
| 220 | `_atom_text()` | Extract text from Atom XML element |
| 225 | `_atom_link()` | Extract href from Atom link element |

### Source-Specific Scrapers
| Line | Function | Source | Method |
|------|----------|--------|--------|
| 237 | `scrape_moneycontrol()` | Moneycontrol | RSS + HTML fallback |
| 269 | `scrape_economictimes()` | Economic Times | RSS + HTML fallback |
| 303 | `scrape_business_standard()` | Business Standard | RSS (HTML often 403) |
| 340 | `scrape_livemint()` | Livemint | RSS + HTML fallback |
| 371 | `scrape_ndtv_profit()` | NDTV Profit | Feedburner RSS + HTML |
| 402 | `scrape_financial_express()` | Financial Express | HTML only (RSS disabled) |
| 430 | `scrape_the_hindu_businessline()` | Hindu BusinessLine | RSS only |
| 435 | `scrape_zeebiz()` | Zee Business | RSS + HTML (Akamai CDN blocks RSS) |
| 466 | `scrape_business_today()` | Business Today | RSS only |
| 472 | `scrape_inc42()` | Inc42 | RSS only (startup funding) |
| 477 | `scrape_sebi()` | SEBI | RSS only (regulatory) |
| 482 | `scrape_investing_india()` | Investing.com India | RSS only |

### NSE Corporate Announcements (Early Signals)
| Line | Function | Purpose |
|------|----------|---------|
| 491 | `HIGH_VALUE_CATS` | Set of high-impact categories: board outcomes, order wins, appointments, analyst meets, updates |
| 504 | `scrape_nse_announcements()` | Real-time NSE exchange filings via NSE API (`/api/corporate-announcements`) |
| 556 | `scrape_nse_high_value()` | Filters NSE filings to only HIGH_VALUE_CATS |

### Google News RSS
| Line | Function | Purpose |
|------|----------|---------|
| 572 | `GOOGLE_NEWS_RSS` | Single broad Google News RSS query |
| 578 | `search_google_news()` | Fetch from single RSS URL |
| 583 | `GOOGLE_NEWS_QUERIES` | 10 focused queries (stock market, RBI, FII, IPO, commodities, etc.) |
| 597 | `search_google_news_multi()` | Multi-query Google News search with dedup |

### DuckDuckGo Web Search (Fallback)
| Line | Function | Purpose |
|------|----------|---------|
| 626 | `search_duckduckgo()` | Single DDG query → articles with redirect URL extraction |
| 671 | `search_breaking_news()` | 10 targeted DDG queries for enrichment |
| 707 | `_infer_source()` | Map URL domain → human-readable source name (25 sources mapped) |

### Semantic Deduplication
| Line | Function | Purpose |
|------|----------|---------|
| 747 | `_normalize()` | Lowercase, strip punctuation, remove stopwords |
| 764 | `_similarity()` | Jaccard similarity of two normalized strings |
| 775 | `SOURCE_PRIORITY` | Authority ranking: NSE(10) > SEBI(9) > Media(4-5) > Aggregators(2-3) |
| 795 | `_deduplicate_semantic()` | Quality-scored dedup: prefers authoritative source, caps at 100 articles, threshold=0.30 |

### LLM News Analyzer
| Line | Function | Purpose |
|------|----------|---------|
| 843 | `call_llm()` | Route to correct provider based on config |
| 863 | `_call_openai_compatible()` | OpenAI/Groq/DeepSeek/Together API call |
| 890 | `_call_anthropic()` | Anthropic Claude API call |
| 927 | `_call_gemini()` | Google Gemini API call (prepends system msg to user msg — Gemini limitation) |
| 976 | `ANALYSIS_PROMPT` | Comprehensive prompt: stock vs index types, 1-10 scale, NSE rules |
| 1005 | `analyze_articles()` | Two-pass: keyword top-40 → Gemini final ranking → diversity filter |
| 1058 | `_diversify_sources()` | 50-50 NSE/Media split, index articles always included |
| 1084 | `_parse_llm_response()` | 3-strategy JSON parsing: greedymatch → full parse → markdown codeblock |
| 1114 | `_keyword_score()` | Fallback if LLM unavailable: Nifty50 + catalyst keywords → 1-10 score |
| 1229 | `_keyword_reason()` | Generate reason text based on score tier |

### Telegram Notifier
| Line | Function | Purpose |
|------|----------|---------|
| 1244 | `send_telegram()` | Send formatted articles via Telegram Bot API, handles split messages |
| 1281 | `_format_message()` | Two-section HTML: "Stock-Specific Catalysts" + "Index Movers & Breaking News" |
| 1344 | `_split_message()` | Split long messages at paragraph boundaries, max 3800 chars each |

### Main Orchestrator
| Line | Function | Purpose |
|------|----------|---------|
| 1371 | `collect_news()` | Execute all scrapers in tiers, URL dedup, semantic dedup |
| 1443 | `get_time_label()` | Determine IST time slot label (uses UTC+5:30) |
| 1454 | `main()` | Entry point: collect → analyze → send → print summary |

---

## 6. DEPLOYMENT

### GitHub Actions (Primary — FREE 24/7)
**File:** `.github/workflows/market_agent.yml`

**Schedule (UTC → IST):**
| Cron (UTC) | IST Time | Label |
|------------|----------|-------|
| `30 3 * * *` | 9:00 AM | Pre-Market |
| `30 8 * * *` | 2:00 PM | Mid-Session |
| `30 13 * * *` | 7:00 PM | Post-Market |

**Secrets required (GitHub → Settings → Secrets & Variables → Actions):**
- `GEMINI_API_KEY` — Google AI Studio free key
- `TELEGRAM_BOT_TOKEN` — From @BotFather
- `TELEGRAM_CHAT_ID` — From @userinfobot

**Env vars in workflow:** `LLM_PROVIDER=gemini`, `LLM_MODEL=gemini-2.5-flash-lite`

**Timeout:** 10 minutes. **Runner:** ubuntu-latest, Python 3.12.

**Note:** GitHub Actions cron is "best effort" — typical delays of 0-30 min. No SLA.

### Local Cron (Alternative)
- `run_agent.sh` sources `.env`, sets up logs, calls agent
- `setup_cron.sh` checks deps, warns about placeholder values
- Cron expressions in setup_cron.sh are for **UTC** (same as GitHub Actions)

---

## 7. COMPLETE GIT HISTORY & CHANGES

| Commit | Date | Description |
|--------|------|-------------|
| `e44605d` | Jun 6, 12:36 | **Initial commit.** 9 sources, Gemini 2.0 Flash, Telegram, GitHub Actions |
| `86b77c8` | Jun 6, 12:43 | **Fix Gemini API.** Gemini doesn't support `systemInstruction`, prepended to user msg |
| `f577155` | Jun 6, 13:17 | **Gemini working.** Two-pass ranking, keyword pre-filter(30) → Gemini(7), JSON regex fix |
| `4289553` | Jun 6, 13:28 | **Major upgrade.** NSE corporate filings scraper, semantic dedup (Jaccard), stock-specific ranking, Nifty 50 keyword scoring |
| `3698d91` | Jun 6, 13:39 | **Diversity filter.** Capped NSE at 4, mix from all sources, top 13 |
| `c099b83` | Jun 6, 13:52 | **Two-section format.** 50-50 NSE/media split, stricter dedup threshold (0.28→0.30) |
| `7e04ab3` | Jun 8, 00:06 | **Timezone fix + new sources.** Fixed `get_time_label()` to use IST (UTC+5:30), added Business Today, Inc42, SEBI, Investing.com India, improved NDTV (Feedburner) and Financial Express (HTML-only) scrapers, enhanced keyword scoring |
| `(next)` | Jun 8, 00:XX | **BUG FIXES.** (1) Gemini auth changed from query-param `?key=` to header `x-goog-api-key` — 403 error was silently killing LLM ranking. (2) Keyword fallback paths in `analyze_articles()` now respect `MIN_IMPACT_SCORE` filter. |

### Uncommitted Changes (none at present — all pushed)
All changes committed and pushed to `origin/main`.

---

## 8. CONVERSATION & DECISION HISTORY

### Session 1: Timezone Bug Report
- **User reported:** Messages arriving at odd timings
- **Diagnosis:**
  - No local cron was set up (confirmed via `crontab -l`)
  - GitHub Actions cron was correctly configured (30 3/8/13 UTC = 9/14/19 IST)
  - `get_time_label()` used `datetime.now()` which returns UTC on GitHub runners
  - Labels were wrong: 2PM run showed "9AM", 7PM run showed "2PM"
  - GitHub Actions cron delays (0-30 min) made timing unpredictable
- **Fix applied:** Changed to `datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)` in `get_time_label()`
- **Committed & pushed:** `7e04ab3`

### Session 2: Project Memory File Created
- **User requested:** A file containing every detail about the project for future AI recall
- **Result:** This file (`PROJECT_MEMORY.md`) — comprehensive project memory

### Session 3: Pre-Flight Check for 9AM Go-Live (Jun 8, 00:XX IST)
- **User requested:** Deep analysis before 9AM IST trigger
- **Bugs found & fixed:**
  1. **Gemini auth broken** — `_call_gemini()` used `?key=` query param but API requires `x-goog-api-key` header. All Gemini calls silently returned 403 and fell back to keyword scoring. **Verified fixed with live API test.**
  2. **Keyword fallback ignores MIN_IMPACT_SCORE** — All 3 fallback paths in `analyze_articles()` returned raw keyword scores without filtering by `MIN_IMPACT_SCORE=6`. Low-scoring noise (1/10) could pass through. **Fixed by adding `_filter_by_min_score()` helper.**
- **Full test results:**
  - ✅ Gemini API (header auth) — working, returns 200
  - ✅ Telegram Bot (`@FinanceNewssbot`) — working, message sent
  - ✅ Config loading — all values correct
  - ✅ Syntax/AST — no errors
  - ✅ All imports resolve
  - ✅ HEAD pushed to `origin/main` (7e04ab3)
- **Committed & pushed** as next commit after 7e04ab3

### Key Decisions Made
1. **Gemini 2.5 Flash Lite** chosen as LLM — free tier, 60 req/min, 1500 req/day
2. **Two-pass ranking** — Keyword pre-filter (cheap) → Gemini (expensive but accurate on shortlist)
3. **50-50 diversity split** — NSE filings vs media sources, index articles always included
4. **Semantic dedup threshold** 0.30 — balances catching duplicates vs. keeping unique stories
5. **NSE API** (`/api/corporate-announcements`) used instead of scraping NSE website
6. **DuckDuckGo** for fallback web search (no API key needed)
7. **GitHub Actions** preferred over local cron for 24/7 reliability

---

## 9. KNOWN ISSUES & LIMITATIONS

### GitHub Actions Cron Delays
- GH Actions cron is "best effort" — delays of 0-30 minutes are normal
- **Cannot be fixed** — platform limitation
- Mitigation: `workflow_dispatch` available for manual triggers

### Broken/Unreliable Sources
| Source | Issue |
|--------|-------|
| Business Standard RSS | Returns 403 (blocked), HTML also 403 |
| NDTV Profit old RSS | 403, switched to Feedburner |
| Financial Express RSS | 410 Gone, HTML-only fallback works |
| Zee Business RSS | Behind Akamai CDN, RSS often fails |
| Moneycontrol RSS | Not well-formed, HTML fallback needed |

### Gemini Auth Method (CRITICAL — Was Broken)
- **Original bug:** Code used `?key={api_key}` query-param auth (line 932)
- **Issue:** Google Gemini API requires `x-goog-api-key` header, not query param
- **Result:** All Gemini calls returned 403 — LLM analysis silently failed, fell back to keyword scoring
- **Fix:** Changed to `headers={"x-goog-api-key": api_key}` on the POST request
- **Verified:** Gemini API now returns 200 ✅

### Gemini JSON Parsing
- Gemini occasionally returns malformed JSON (extra text outside array)
- 3-strategy fallback: greedy regex → full parse → code block extraction
- Falls back to keyword scoring if all strategies fail

### Gemini Rate Limits
- 60 requests/minute, 1500 requests/day on free tier
- Currently only 1 request per run (40 candidates → 1 API call)
- Should stay within limits with 3 runs/day

### MIN_IMPACT_SCORE Not Applied in Keyword Fallback
- **Original bug:** All 3 keyword fallback paths in `analyze_articles()` (LLM not configured, empty response, parse failure) returned `_keyword_score(articles, top_n)` without filtering by `MIN_IMPACT_SCORE`
- **Result:** Articles scoring as low as 1/10 could appear in Telegram output when LLM was unavailable
- **Fix:** Added `_filter_by_min_score()` helper applied to all fallback return paths
- **Note:** The LLM path already correctly filtered at line 1048-1049; this only affected fallback scenarios

### No README.md
- Project lacks a standard README for GitHub visitors
- (User has not requested one — do not create unless asked)

---

## 10. LOGS SUMMARY

### agent.log (cumulative, 705 lines)
- **Jun 5, 21:35** — First test run. Only Hindu BusinessLine RSS worked (60 articles). LLM and Telegram not configured.
- **Jun 5, 21:37** — Second test run with Google News RSS added. 382 articles from Google. Still no LLM/Telegram.
- **Jun 6, 13:19** — Full run. 593 articles, Gemini ranked 8 (min score 5), Telegram sent 7 (2 messages). Working end-to-end.
- **Jun 6, 14:52** — Full run. 1321 articles (after dedup: 100), Gemini JSON parse failed (malformed response), fallback to keyword scoring, 15 articles sent via Telegram.

### run_2026-06-06_13-19-28.log
Successful run with top headlines about RBI rate decision. All 7 articles had impact scores 7-10/10.

---

## 11. SENSITIVE DATA (DO NOT EXPOSE)

- **GitHub Token:** Exposed in git remote URL earlier — needs rotation (create new PAT and update remote)
- **Gemini API Key:** Stored in `.env` as `GEMINI_API_KEY`, also in GitHub Secrets
- **Telegram Bot Token:** Stored in `.env` as `TELEGRAM_BOT_TOKEN`, also in GitHub Secrets
- **Telegram Chat ID:** Stored in `.env` as `TELEGRAM_CHAT_ID`, also in GitHub Secrets

---

## 12. QUICK REFERENCE

### Common Commands
```bash
# Run locally (from project dir)
./run_agent.sh

# Run without shell wrapper
python3 indian_market_agent.py

# View logs
tail -f logs/agent.log

# Check last run log
ls -t logs/run_*.log | head -1 | xargs cat

# Deploy to GitHub
git push origin main
```

### Key URLs
- **Gemini API Key:** https://aistudio.google.com/apikey
- **Telegram BotFather:** https://t.me/BotFather
- **Telegram User Info Bot:** https://t.me/userinfobot
- **GitHub Repo:** https://github.com/cherishdhingra12/indian-market-agent
- **GitHub Actions:** https://github.com/cherishdhingra12/indian-market-agent/actions

### To Restore Context After Fresh Start
1. Read `PROJECT_MEMORY.md` (this file)
2. Read `indian_market_agent.py` (full source)
3. Read `config.py`
4. Read `logs/agent.log` (last 50 lines)
5. Check `git log --oneline -5`
6. Read `.github/workflows/market_agent.yml`
