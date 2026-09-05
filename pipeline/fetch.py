"""Discover candidate news articles about repeat offenders.

Two sources:
  - GDELT DOC 2.0 API: free full-text news search with direct article URLs.
    Works for both daily updates and historical backfill (coverage to 2017).
  - Google News RSS: catches smaller local outlets GDELT sometimes misses.
    Google wraps links in redirect URLs; we decode the older base64 format
    when possible and otherwise keep the Google link as the source URL.

Ported from flock-crime-tracker, with two changes for a topic that is a
concept rather than a brand name: per-query hit accounting (QUERY_HITS), and
window splitting when a GDELT query saturates the 250-record cap.
"""

import base64
import json
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote, urlsplit

import feedparser
import requests
import trafilatura
from googlenewsdecoder import gnewsdecoder
from trafilatura.settings import use_config

from config import GDELT_QUERIES, GOOGLE_NEWS_QUERIES, QUERY_STATS_JSON

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
    # GDELT throttles GitHub Actions' shared egress IPs hard: in the first
    # live runs about half the queries 429'd through three attempts with
    # 45-135s backoffs, costing ~5 minutes each for nothing. Two attempts,
    # short backoff: GDELT is a supplement to Google News on the daily run.
    for attempt in range(2):
        try:
            resp = requests.get(GDELT_DOC_API, params=params,
                                headers={"User-Agent": USER_AGENT}, timeout=30)
            if resp.status_code == 429:
                GDELT_STATS["retries"] += 1
                time.sleep(15)
                continue
            resp.raise_for_status()
            articles = resp.json().get("articles", [])
            break
        except (requests.RequestException, ValueError) as e:
            print(f"  GDELT error for {query!r}: {e}")
            time.sleep(5)
    if articles is None:
        GDELT_STATS["gave_up"] += 1
        print(f"  GDELT gave up on {query!r}")
        return []

    h = _hits(query)
    h["gdelt"] += len(articles)
    if len(articles) >= max_records:
        h["gdelt_capped"] = True

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


GDELT_SLEEP = 8  # GDELT asks for ~1 request per 5s; 6 was not enough
MIN_SPLIT_HOURS = 6


def gdelt_search_split(query: str, start: datetime, end: datetime,
                       depth: int = 0) -> list[dict]:
    """gdelt_search, but when a window returns the full 250-record cap, re-run
    the query on each half of the window so nothing past the cap is lost.
    Stops splitting below MIN_SPLIT_HOURS: a window that dense is a query
    that needs narrowing, not more calls."""
    out = gdelt_search(query, start, end)
    hours = (end - start).total_seconds() / 3600
    if len(out) < GDELT_MAX_RECORDS * 0.9 or hours < MIN_SPLIT_HOURS * 2 or depth >= 4:
        return out
    time.sleep(GDELT_SLEEP)
    mid = start + (end - start) / 2
    left = gdelt_search_split(query, start, mid, depth + 1)
    time.sleep(GDELT_SLEEP)
    right = gdelt_search_split(query, mid, end, depth + 1)
    seen = {c["url"] for c in left}
    return left + [c for c in right if c["url"] not in seen]


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


def discover_daily(days_back: int = 1, use_gdelt: bool | None = None) -> list[dict]:
    """All candidates from the last `days_back` days, deduped by URL.

    GDELT is off by default on the daily run: from GitHub Actions' shared
    egress IPs it 429s nearly every query and returned 47 of ~2,600
    candidates across three live runs. Set USE_GDELT=1 to enable it (it
    works from a normal residential or office IP)."""
    if use_gdelt is None:
        use_gdelt = os.environ.get("USE_GDELT", "0") == "1"
    end = datetime.now(timezone.utc).replace(tzinfo=None)
    start = end - timedelta(days=days_back)
    candidates: dict[str, dict] = {}
    t0 = time.monotonic()
    for q in GDELT_QUERIES if use_gdelt else []:
        found = gdelt_search_split(q, start, end)
        for c in found:
            candidates.setdefault(c["url"], c)
        print(f"  GDELT {len(found):4} | {q[:80]}")
        time.sleep(GDELT_SLEEP)
    for q in GOOGLE_NEWS_QUERIES:
        found = google_news_search(q)
        for c in found:
            candidates.setdefault(c["url"], c)
        time.sleep(1)
    print(f"  Google News: {sum(h['gnews'] for h in QUERY_HITS.values())} items "
          f"across {len(GOOGLE_NEWS_QUERIES)} queries")
    print(f"  GDELT calls {GDELT_STATS['calls']}, retries {GDELT_STATS['retries']}, "
          f"gave up {GDELT_STATS['gave_up']}; discovery took {int(time.monotonic() - t0)}s")
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

# A slow site must not stall a worker for trafilatura's default 30s.
FETCH_TIMEOUT = 15
_TRAFILATURA_CONFIG = use_config()
_TRAFILATURA_CONFIG.set("DEFAULT", "DOWNLOAD_TIMEOUT", str(FETCH_TIMEOUT))


MIN_TEXT_CHARS = 500  # a video-page blurb is not an article
VIDEO_PATH_FRAGMENTS = ("/video/", "/videos/", "/watch/", "/clip/")


def fetch_article_text(url: str, max_chars: int = 14000) -> str | None:
    """Download and extract the main text of an article. None on failure.
    Tries trafilatura's fetch first, then a browser-UA request: many station
    sites 403 obvious non-browser agents."""
    if "news.google.com" in url:
        return None
    if any(f in url.lower() for f in VIDEO_PATH_FRAGMENTS):
        return None
    try:
        downloaded = trafilatura.fetch_url(url, config=_TRAFILATURA_CONFIG)
        if not downloaded:
            resp = requests.get(url, timeout=FETCH_TIMEOUT, headers={"User-Agent": BROWSER_UA})
            if not resp.ok:
                return None
            downloaded = resp.text
        text = trafilatura.extract(downloaded, include_comments=False)
        if text and len(text) >= MIN_TEXT_CHARS:
            return text[:max_chars]
    except Exception:
        pass
    return None
