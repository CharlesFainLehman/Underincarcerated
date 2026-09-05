"""Discover candidate news articles about repeat offenders.

Two sources:
  - GDELT DOC 2.0 API: free full-text news search with direct article URLs.
    Works for both daily updates and historical backfill (coverage to 2017).
  - Google News RSS: catches smaller local outlets GDELT sometimes misses.
    Google wraps links in redirect URLs; we decode the older base64 format
    when possible and otherwise keep the Google link as the source URL.

Ported from flock-crime-tracker. The one addition is per-query hit
accounting (QUERY_HITS), because "repeat offender" queries saturate GDELT's
250-record cap in a way "Flock camera" never did.
"""

import base64
import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote, urlsplit

import feedparser
import requests
import trafilatura
from googlenewsdecoder import gnewsdecoder

from config import QUERY_STATS_JSON, SEARCH_QUERIES

GDELT_DOC_API = "https://api.gdeltproject.org/api/v2/doc/doc"
GDELT_MAX_RECORDS = 250
# Plain browser UA: GDELT's front end rejects non-browser user agents.
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

GDELT_STATS = {"calls": 0, "retries": 0, "gave_up": 0}
# query -> {"gdelt": n, "gdelt_capped": bool, "gnews": n} for the current run.
QUERY_HITS: dict[str, dict] = {}

# Domains that produce noise, not news coverage: wires, press-release hosts,
# and police-blotter aggregators that reprint booking logs without reporting.
SKIP_DOMAINS = ("prnewswire.com", "businesswire.com", "globenewswire.com",
                "streetinsider.com", "einpresswire.com", "newswire.com",
                "arrests.org", "mugshots.com", "bustednewspaper.com",
                "jailbase.com", "crimegrade.org")

SKIP_URL_FRAGMENTS = ("newswire", "press_release", "press-release", "online_features",
                      "/mugshots/", "/arrest-log", "/arrest-report", "/police-blotter",
                      "/blotter/", "/booking-report")


def is_vendor_or_wire(url: str, domain: str = "") -> bool:
    """True for wire syndication, press releases, and blotter aggregators.
    validate_data.py rejects these as primary sources, so every discovery
    path must filter them or the daily commit step fails on its own data."""
    low = (url or "").lower()
    host = urlsplit(low).netloc if "://" in low else ""
    if any(skip in host for skip in SKIP_DOMAINS):
        return True
    if domain:
        hay = domain.lower()
        stems = [d.rsplit(".", 1)[0] for d in SKIP_DOMAINS]
        if any(skip in hay for skip in SKIP_DOMAINS) or any(st in hay for st in stems):
            return True
    return any(k in low for k in SKIP_URL_FRAGMENTS)


def _normalize(candidate: dict) -> dict:
    candidate["url"] = candidate["url"].strip()
    candidate["title"] = re.sub(r"\s+", " ", candidate.get("title", "")).strip()
    return candidate


def _hits(query: str) -> dict:
    return QUERY_HITS.setdefault(query, {"gdelt": 0, "gdelt_capped": False, "gnews": 0})


def gdelt_search(query: str, start: datetime, end: datetime,
                 max_records: int = GDELT_MAX_RECORDS) -> list[dict]:
    """Full-text search of GDELT's news archive for a date window."""
    params = {
        "query": f"{query} sourcelang:english sourcecountry:US",
        "mode": "artlist",
        "format": "json",
        "maxrecords": str(max_records),
        "sort": "datedesc",
        "startdatetime": start.strftime("%Y%m%d%H%M%S"),
        "enddatetime": end.strftime("%Y%m%d%H%M%S"),
    }
    GDELT_STATS["calls"] += 1
    articles = None
    for attempt in range(3):
        try:
            resp = requests.get(GDELT_DOC_API, params=params,
                                headers={"User-Agent": USER_AGENT}, timeout=30)
            if resp.status_code == 429:
                GDELT_STATS["retries"] += 1
                time.sleep(45 * (attempt + 1))
                continue
            resp.raise_for_status()
            articles = resp.json().get("articles", [])
            break
        except (requests.RequestException, ValueError) as e:
            print(f"  GDELT error for {query!r}: {e}")
            time.sleep(10)
    if articles is None:
        GDELT_STATS["gave_up"] += 1
        print(f"  GDELT gave up on {query!r}")
        return []

    h = _hits(query)
    h["gdelt"] += len(articles)
    if len(articles) >= max_records:
        h["gdelt_capped"] = True
        print(f"  GDELT cap hit for {query!r} ({start:%Y-%m-%d}..{end:%Y-%m-%d}); "
              f"narrow the query or the window")

    out = []
    for a in articles:
        domain = a.get("domain", "")
        url = a.get("url", "")
        if is_vendor_or_wire(url, domain):
            continue
        seendate = a.get("seendate", "")  # e.g. 20250811T120000Z
        published = ""
        if len(seendate) >= 8:
            published = f"{seendate[:4]}-{seendate[4:6]}-{seendate[6:8]}"
        out.append(_normalize({
            "url": url,
            "title": a.get("title", ""),
            "source": domain,
            "published": published,
            "snippet": "",
            "query": query,
        }))
    return out


def _decode_google_news_url(url: str) -> str | None:
    """Best-effort offline decode of a Google News redirect URL."""
    m = re.search(r"news\.google\.com/rss/articles/([^?/]+)", url)
    if not m:
        return None
    try:
        raw = base64.urlsafe_b64decode(m.group(1) + "==")
        text = raw.decode("latin-1", errors="ignore")
        urls = re.findall(r"https?://[^\x00-\x1f\x7f-\xff\"]+", text)
        for u in urls:
            if "news.google.com" not in u:
                return u.rstrip("R")
    except Exception:
        pass
    return None


def google_news_search(query: str) -> list[dict]:
    feed_url = ("https://news.google.com/rss/search?q="
                f"{quote(query)}&hl=en-US&gl=US&ceid=US:en")
    feed = feedparser.parse(feed_url)
    out = []
    for entry in feed.entries:
        link = entry.get("link", "")
        decoded = _decode_google_news_url(link)
        source = entry.get("source", {}).get("title", "")
        published = ""
        if entry.get("published_parsed"):
            published = time.strftime("%Y-%m-%d", entry.published_parsed)
        snippet = re.sub(r"<[^>]+>", " ", entry.get("summary", ""))
        if is_vendor_or_wire(decoded or "", source.replace(" ", "").lower()):
            continue
        out.append(_normalize({
            "url": decoded or link,
            "title": entry.get("title", ""),
            "source": source,
            "published": published,
            "snippet": snippet,
            "query": query,
        }))
    _hits(query)["gnews"] += len(out)
    return out


def discover_daily(days_back: int = 1) -> list[dict]:
    """All candidates from the last `days_back` days, both sources, deduped by URL."""
    end = datetime.now(timezone.utc).replace(tzinfo=None)
    start = end - timedelta(days=days_back)
    candidates: dict[str, dict] = {}
    for q in SEARCH_QUERIES:
        for c in gdelt_search(q, start, end):
            candidates.setdefault(c["url"], c)
        time.sleep(6)  # GDELT asks for ~1 request per 5s
    for q in SEARCH_QUERIES:
        for c in google_news_search(q):
            candidates.setdefault(c["url"], c)
        time.sleep(1)
    return list(candidates.values())


def save_query_stats() -> None:
    """Append this run's per-query hit counts to data/query_stats.json,
    keyed by date. Used to prune and split queries."""
    stats = {}
    if QUERY_STATS_JSON.exists():
        stats = json.loads(QUERY_STATS_JSON.read_text(encoding="utf-8"))
    stats[date.today().isoformat()] = QUERY_HITS
    # Keep the last 90 days.
    for k in sorted(stats)[:-90]:
        del stats[k]
    QUERY_STATS_JSON.parent.mkdir(parents=True, exist_ok=True)
    QUERY_STATS_JSON.write_text(json.dumps(stats, indent=1, sort_keys=True), encoding="utf-8")


def resolve_candidate(candidate: dict) -> None:
    """Decode a Google News redirect URL to the real article URL, in place."""
    url = candidate["url"]
    if "news.google.com" not in url:
        return
    try:
        res = gnewsdecoder(url, interval=1)
        decoded = res.get("decoded_url") if res.get("status") else None
        if decoded and "news.google.com" not in decoded:
            candidate["google_url"] = url
            candidate["url"] = decoded.strip()
    except Exception:
        pass


BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def fetch_article_text(url: str, max_chars: int = 14000) -> str | None:
    """Download and extract the main text of an article. None on failure.
    Tries trafilatura's fetch first, then a browser-UA request: many station
    sites 403 obvious non-browser agents."""
    if "news.google.com" in url:
        return None
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            resp = requests.get(url, timeout=30, headers={"User-Agent": BROWSER_UA})
            if not resp.ok:
                return None
            downloaded = resp.text
        text = trafilatura.extract(downloaded, include_comments=False)
        if text and len(text) > 200:
            return text[:max_chars]
    except Exception:
        pass
    return None
