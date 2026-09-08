"""Second-source search: find another outlet's report of each stored story.

    python pipeline/corroborate.py [--limit N] [--recheck] [--max-minutes M]

For every row without a second distinct outlet (or every unchecked row),
query Google News for the offender's name and place inside a window around
the incident date, keep one candidate per outlet other than the primary
source, fetch each candidate, and ask the model whether the article covers
the same person and the same incident. Confirmed matches are appended to
additional_sources. The row is stamped corroboration_checked so the search
is not repeated; a row that gains nothing stays single-source and is still
stamped. Stops early, without stamping, when Google throttles redirect
decoding, so the next run resumes where this one stopped.

The daily run calls this for new rows only; the manual workflow sweeps the
whole database. Rows whose primary source is on TRUSTED_OUTLETS (config)
are skipped unless --include-trusted is given.
"""

import argparse
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Optional
from urllib.parse import urlsplit

import anthropic
from pydantic import BaseModel, ValidationError

from config import BACKFILL_STORIES_CSV, DEDUPE_MODEL, STORIES_CSV, TRUSTED_OUTLETS
from fetch import fetch_article_text, google_news_search, is_vendor_or_wire, resolve_candidate
from process import DecisionLog, canonical_url, default_decision_log, syndication_path
from store import load_stories, save_stories

MAX_CANDIDATES = 4      # outlets tried per row
WORKERS = 4             # rows in flight (each decodes, fetches, and asks in turn)
CHECKPOINT_EVERY = 25
THROTTLE_MIN = 20       # decodes attempted before the throttle check applies
THROTTLE_SHARE = 0.8    # ...and the share that must fail to stop the run


class Match(BaseModel):
    same_person: bool
    same_incident: bool
    reason: str


def _host(url: str) -> str:
    return urlsplit(url).netloc.lower().removeprefix("www.")


def outlets(row: dict) -> set[str]:
    return {_host(u) for u in [row.get("source_url", "")] + (row.get("additional_sources") or "").split() if u}


def is_trusted(url: str) -> bool:
    """Primary source is an outlet on the trusted list (or a subdomain of one)."""
    host = _host(url)
    return any(host == d or host.endswith("." + d) for d in TRUSTED_OUTLETS)


def publishable(row: dict) -> bool:
    """Two or more distinct outlets, or a trusted primary source."""
    return is_trusted(row.get("source_url", "")) or len(outlets(row)) >= 2


def pending(row: dict) -> bool:
    """Not yet publishable, but the second-source search has not run on it."""
    return not publishable(row) and not row.get("corroboration_checked")


def needs_check(row: dict, recheck: bool = False, include_trusted: bool = False) -> bool:
    if not row.get("offender_name"):
        return False
    if not include_trusted and is_trusted(row.get("source_url", "")):
        return False
    if recheck:
        return True
    return not row.get("corroboration_checked") and len(outlets(row)) < 2


def build_query(row: dict) -> str:
    """Quoted full name plus the city, or the state when no city is known."""
    name = re.sub(r"\s+", " ", row["offender_name"]).strip()
    place = (row.get("city") or "").strip() or (row.get("state") or "").strip()
    return f'"{name}" {place}'.strip()


def date_window(row: dict) -> tuple[Optional[datetime], Optional[datetime]]:
    """Wide enough to catch follow-up coverage (charging, arraignment) but
    narrow enough to exclude an earlier or later incident: 45 days after a
    full date, a month plus 45 days after a month, nothing for a bare year."""
    v = (row.get("incident_date") or "").strip()
    try:
        if len(v) == 10:
            d = datetime.strptime(v, "%Y-%m-%d")
            return d - timedelta(days=7), d + timedelta(days=45)
        if len(v) == 7:
            d = datetime.strptime(v, "%Y-%m")
            return d - timedelta(days=7), d + timedelta(days=76)
    except ValueError:
        pass
    return None, None


def find_candidates(row: dict, hits: list[dict]) -> list[dict]:
    """One candidate per outlet, excluding the primary source's outlet, wire
    and vendor copies, and syndicated copies of the primary article."""
    have = outlets(row)
    primary_path = syndication_path(row.get("source_url", ""))
    primary = canonical_url(row.get("source_url", ""))
    seen_hosts: set[str] = set()
    out = []
    for h in hits:
        url = h.get("url", "")
        if not url or "news.google.com" in url:
            # Still a redirect: outlet unknown until decoded. Keep, judged later.
            out.append(h)
            continue
        host = _host(url)
        if host in have or host in seen_hosts or is_vendor_or_wire(url, h.get("source", "")):
            continue
        if canonical_url(url) == primary or (primary_path and syndication_path(url) == primary_path):
            continue
        seen_hosts.add(host)
        out.append(h)
    return out


def confirm(client: anthropic.Anthropic, row: dict, cand: dict, text: str) -> Optional[Match]:
    record = (f"name: {row['offender_name']} | age: {row.get('age') or '?'} | "
              f"place: {row.get('city')}, {row.get('state')} | incident date: {row.get('incident_date') or '?'} | "
              f"new offense: {row.get('new_offense_type')} | summary: {row.get('summary')}")
    try:
        resp = client.messages.parse(
            model=DEDUPE_MODEL, max_tokens=300,
            system=("You check whether a news article reports the same person and the same "
                    "incident as a database record. Same incident means the same new offense "
                    "or arrest, at any stage (arrest, charging, arraignment, plea, sentencing). "
                    "A different arrest of the same person is same_person but not same_incident. "
                    "A different person with the same name is neither. Be strict: when the "
                    "article does not clearly match, say false."),
            messages=[{"role": "user", "content":
                       f"RECORD:\n{record}\n\nARTICLE ({cand.get('source')}, {cand.get('published')}):\n"
                       f"Headline: {cand.get('title')}\n\n{text[:5000]}"}],
            output_format=Match,
        )
        return resp.parsed_output
    except (ValidationError, anthropic.APIError):
        return None


class Throttled(RuntimeError):
    pass


def corroborate_row(client: anthropic.Anthropic, row: dict, log: DecisionLog,
                    stats: dict) -> list[str]:
    """Confirmed second-source URLs for one row. Raises Throttled when the
    redirect decoder is being refused, so the caller can stop cleanly."""
    start, end = date_window(row)
    hits = google_news_search(build_query(row), start, end)
    found: list[str] = []
    tried_hosts = outlets(row)
    for cand in find_candidates(row, hits):
        if len(found) + len(tried_hosts) - len(outlets(row)) >= MAX_CANDIDATES:
            break
        if "news.google.com" in cand["url"]:
            stats["decodes"] += 1
            resolve_candidate(cand)
            if "news.google.com" in cand["url"]:
                stats["undecoded"] += 1
                if stats["decodes"] >= THROTTLE_MIN and stats["undecoded"] / stats["decodes"] > THROTTLE_SHARE:
                    raise Throttled()
                continue
            host = _host(cand["url"])
            if host in tried_hosts or is_vendor_or_wire(cand["url"], cand.get("source", "")):
                continue
        else:
            host = _host(cand["url"])
        tried_hosts.add(host)
        text = fetch_article_text(cand["url"])
        if not text:
            log.write(stage="corroborate", id=row["id"], url=cand["url"], result="no_text")
            continue
        m = confirm(client, row, cand, text)
        log.write(stage="corroborate", id=row["id"], url=cand["url"],
                  same_person=m.same_person if m else None,
                  same_incident=m.same_incident if m else None, reason=m.reason if m else "no answer")
        if m and m.same_incident:
            found.append(cand["url"])
    return found


def sweep(client: anthropic.Anthropic, path, limit: int = 0, recheck: bool = False,
          max_minutes: float = 0, log: DecisionLog | None = None,
          include_trusted: bool = False) -> dict:
    log = log or DecisionLog(default_decision_log())
    stories = load_stories(path)
    todo = [s for s in stories if needs_check(s, recheck, include_trusted)]
    if limit:
        todo = todo[:limit]
    counts = {"checked": 0, "corroborated": 0, "sources_added": 0, "throttled": False}
    if not todo:
        return counts
    print(f"{path.name}: searching for second sources on {len(todo)} of {len(stories)} stories")
    stats = {"decodes": 0, "undecoded": 0}
    t0 = time.monotonic()

    def work(s):
        try:
            return corroborate_row(client, s, log, stats)
        except Throttled:
            return "throttled"
        except Exception as e:  # noqa: BLE001
            print(f"  error on id {s['id']}: {e}")
            return None

    today = date.today().isoformat()
    for i in range(0, len(todo), CHECKPOINT_EVERY):
        if max_minutes and (time.monotonic() - t0) / 60 > max_minutes:
            print("  time budget reached; stopping")
            break
        chunk = todo[i:i + CHECKPOINT_EVERY]
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            results = list(pool.map(work, chunk))
        for s, found in zip(chunk, results):
            if found is None or found == "throttled":
                continue
            counts["checked"] += 1
            s["corroboration_checked"] = today
            if found:
                have = (s.get("additional_sources") or "").split()
                new = [u for u in found if u not in have and u != s.get("source_url")]
                if new:
                    s["additional_sources"] = " ".join(have + new)
                    counts["corroborated"] += 1
                    counts["sources_added"] += len(new)
                    print(f"  id {s['id']}: {s['offender_name']} +{len(new)} ({', '.join(_host(u) for u in new)})")
        save_stories(stories, path)
        if "throttled" in results:
            counts["throttled"] = True
            print("  Google is refusing to decode redirects; stopping (unfinished rows stay unchecked)")
            break
    return counts


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=0, help="rows per file (0 = all that need it)")
    ap.add_argument("--recheck", action="store_true", help="search again for rows already checked")
    ap.add_argument("--max-minutes", type=float, default=0, help="stop after this long (0 = no limit)")
    ap.add_argument("--include-trusted", action="store_true",
                    help="also search rows whose primary source is on TRUSTED_OUTLETS")
    a = ap.parse_args()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set; refusing to run.")
    client = anthropic.Anthropic()
    t0 = time.monotonic()
    total = {"checked": 0, "corroborated": 0, "sources_added": 0}
    for path in (STORIES_CSV, BACKFILL_STORIES_CSV):
        if not path.exists():
            continue
        c = sweep(client, path, a.limit, a.recheck, a.max_minutes, include_trusted=a.include_trusted)
        for k in total:
            total[k] += c[k]
        if c["throttled"]:
            break
    print(f"Second sources: {total['corroborated']} of {total['checked']} rows corroborated, "
          f"{total['sources_added']} sources added, in {int(time.monotonic() - t0)}s")


if __name__ == "__main__":
    main()
