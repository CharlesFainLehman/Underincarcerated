"""Shared candidate-processing loop used by the daily run and the backfill.

Order per candidate:
  seen-URL check -> triage (batched, headline only) -> Google redirect
  resolve -> wire/blotter filter -> same-article URL check -> fetch ->
  classify -> evidence-quote check -> dedupe -> append.

Triage runs before URL resolution on purpose: decoding a Google News
redirect costs about a second of HTTP per URL, and the first live run spent
half an hour decoding 2,000 of them before classifying anything. Triage
needs only the headline, outlet, and snippet the feed already gave us.

Everything network-bound runs in thread pools: redirect decoding, and the
fetch + classify + verify step. Only dedupe and append are serial, because
each depends on the rows added before it. Work is done in chunks of
CHECKPOINT_EVERY so a killed run keeps its progress.

Every decision is appended to a JSONL log (data/decisions/, gitignored, and
uploaded as a workflow artifact) so prompt calibration can be done against
real outputs instead of memory.
"""

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import anthropic

from classify import check_evidence, classify_article
from config import DECISIONS_DIR, US_STATES
from dedupe import check_duplicate
from fetch import fetch_article_text, is_vendor_or_wire, resolve_candidate
from store import make_row, next_story_id
from triage import triage_candidates

_VARIANT_SEGMENTS = ("/gallery/", "/newsletter/gallery/", "/newsletter/", "/amp/", "/photos/")

# Query parameters that never identify an article. Everything else is kept:
# some sites key articles entirely by query string.
_TRACKING_PARAMS = ("utm_", "fbclid", "gclid", "mc_cid", "mc_eid", "ref",
                    "ito", "share", "outputType", "taid", "cmpid")


def canonical_url(url: str) -> str:
    """Normalize an article URL for duplicate comparison."""
    u = url.split("#")[0].strip().lower()
    base, _, query = u.partition("?")
    base = base.rstrip("/")
    for seg in _VARIANT_SEGMENTS:
        base = base.replace(seg, "/")
    if query:
        kept = [kv for kv in query.split("&")
                if kv and not any(kv.startswith(t) for t in _TRACKING_PARAMS)]
        if kept:
            return base + "?" + "&".join(sorted(kept))
    return base


_DATED_PATH_RE = re.compile(r"/\d{4}/\d{2}/\d{2}/[^/?]+$")


def syndication_path(url: str) -> str | None:
    """The /YYYY/MM/DD/slug tail of a canonical URL, if any. Newspaper chains
    republish one article at the same dated path on sibling hostnames."""
    m = _DATED_PATH_RE.search(canonical_url(url).partition("?")[0])
    return m.group(0) if m else None


class DecisionLog:
    def __init__(self, path: Path | None):
        self.path = path
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, **record) -> None:
        if not self.path:
            return
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _date_horizon() -> str:
    return (date.today() + timedelta(days=2)).isoformat()


def default_decision_log() -> Path:
    return DECISIONS_DIR / f"{date.today().isoformat()}.jsonl"


def _mark_seen(seen: set[str], candidate: dict) -> None:
    seen.add(candidate["url"])
    if candidate.get("google_url"):
        seen.add(candidate["google_url"])


CHECKPOINT_EVERY = 25
RESOLVE_WORKERS = 4    # parallel Google News redirect decodes (8 tripped Google's throttle)
RESOLVE_RETRY_WAIT = 600  # seconds to wait when Google throttles the decoder, before retrying
RESOLVE_RETRIES = 2


class ResolutionThrottled(RuntimeError):
    """Google refused to decode most redirects even after waiting. Nothing
    in this batch was marked seen; the caller should stop and retry later."""
CLASSIFY_WORKERS = 8   # parallel fetch + classify (I/O bound: HTTP and API)


def _title_key(title: str) -> str:
    """Headline normalized for cross-source matching. Google News appends
    " - Outlet"; strip it, then keep letters and digits only."""
    t = (title or "").rsplit(" - ", 1)[0].lower()
    return re.sub(r"[^a-z0-9]+", "", t)


_COUNT_RE = re.compile(
    r"\b(\d{1,3}|dozen|dozens)\b[^.]{0,40}?\b(prior|previous|arrest|convict|felon|offense|"
    r"times|charges|rap sheet|busts)", re.I)
_LABEL_RE = re.compile(r"repeat offender|career criminal|habitual offender|prolific|"
                       r"serial (thief|burglar|offender)|rap sheet", re.I)
_RELEASE_RE = re.compile(r"out on (bail|bond)|on (parole|probation)|released (early|on bond)|"
                         r"pretrial release|days after (his|her|being) release", re.I)


def headline_priority(candidate: dict) -> int:
    """Higher first when a run is capped. A headline that states a count
    ("man with 12 prior arrests") is the most likely to meet the strict
    threshold; a label or a release phrase is next; everything else last."""
    t = candidate.get("title") or ""
    score = 0
    if _COUNT_RE.search(t):
        score += 4
    if _LABEL_RE.search(t):
        score += 2
    if _RELEASE_RE.search(t):
        score += 1
    return score


def _fetch_and_classify(client, candidate: dict) -> dict:
    """Worker: everything for one candidate that needs no shared state.
    Returns a dict with exactly one of: text=None (no_text), error, or
    (cls, text)."""
    try:
        text = fetch_article_text(candidate["url"])
        if not text:
            return {"no_text": True}
        cls = classify_article(client, candidate, text)
        return {"cls": cls, "text": text}
    except Exception as e:  # noqa: BLE001 - surfaced to the serial loop
        return {"error": e}


def process_candidates(client: anthropic.Anthropic, candidates: list[dict],
                       stories: list[dict], seen_urls: set[str],
                       decision_log: Path | None = None,
                       checkpoint=None, max_classify: int = 0, id_base: int = 0) -> dict:
    """Classify candidates and append qualifying, non-duplicate rows to stories.

    Mutates `stories` and `seen_urls` in place. Returns counts for logging.
    `checkpoint`, if given, is called every CHECKPOINT_EVERY fetched articles
    so a killed run (workflow timeout) keeps its progress. `max_classify`
    caps the number of articles fetched this run (0 = no cap); the rest are
    left unseen and picked up next run.
    """
    counts = {"new": 0, "duplicates": 0, "same_person": 0, "triaged_out": 0,
              "rejected": 0, "no_text": 0, "unresolved": 0, "skipped_seen": 0, "errors": 0}
    log = DecisionLog(decision_log)

    stored = {canonical_url(s_["source_url"]) for s_ in stories}
    stored |= {canonical_url(u) for s_ in stories
               for u in s_.get("additional_sources", "").split() if u}
    stored_paths = {p for s_ in stories
                    for u in [s_["source_url"], *s_.get("additional_sources", "").split()]
                    for p in [syndication_path(u)] if p}

    # Pass 1: drop anything already seen. No model calls, no HTTP.
    fresh = []
    for candidate in candidates:
        if candidate["url"] in seen_urls:
            counts["skipped_seen"] += 1
        else:
            fresh.append(candidate)

    # Pass 2: batched headline triage on the feed's own title and snippet.
    if fresh:
        try:
            keep = triage_candidates(client, fresh)
        except Exception as e:
            print(f"  triage failed ({e}); fetching everything")
            keep = [True] * len(fresh)
    else:
        keep = []
    kept = []
    for candidate, k in zip(fresh, keep):
        log.write(stage="triage", url=candidate["url"], title=candidate.get("title"),
                  query=candidate.get("query"), keep=k)
        if k:
            kept.append(candidate)
        else:
            _mark_seen(seen_urls, candidate)
            counts["triaged_out"] += 1
    print(f"Triage kept {len(kept)} of {len(fresh)} fresh candidates")
    if max_classify:
        # Best headlines first, and no more redirect decoding than the cap
        # can use (with a margin for URLs that turn out to be duplicates).
        kept.sort(key=headline_priority, reverse=True)
        kept = kept[:int(max_classify * 1.3) + 5]

    # Pass 3: resolve Google redirects for kept candidates only, in parallel,
    # skipping any whose headline duplicates a direct-URL candidate (the same
    # article reached via GDELT; decoding it would only find a duplicate).
    t0 = time.monotonic()
    direct_titles = {_title_key(c["title"]) for c in kept if "news.google.com" not in c["url"]}
    google = []
    for c in kept:
        if "news.google.com" in c["url"] and _title_key(c["title"]) in direct_titles:
            _mark_seen(seen_urls, c)
            counts["duplicates"] += 1
        elif "news.google.com" in c["url"]:
            google.append(c)
    if google:
        print(f"Resolving {len(google)} Google News redirects "
              f"({len(kept) - len(google)} direct or headline-duplicate)...")
        pending = google
        for attempt in range(RESOLVE_RETRIES + 1):
            with ThreadPoolExecutor(max_workers=RESOLVE_WORKERS) as pool:
                list(pool.map(resolve_candidate, pending))
            pending = [c for c in pending if "news.google.com" in c["url"]]
            print(f"  resolved {len(google) - len(pending)}/{len(google)} "
                  f"in {int(time.monotonic() - t0)}s")
            if len(pending) <= max(2, 0.5 * len(google)):
                break
            # Google throttles its decoder after ~1,500 decodes in an hour.
            # In the first backfill that silently cost 78 weeks: every
            # undecoded URL was treated as "no text" and marked seen. Now
            # wait and retry, and never mark an undecoded URL seen.
            if attempt < RESOLVE_RETRIES:
                print(f"  {len(pending)} still undecoded: Google is throttling; "
                      f"waiting {RESOLVE_RETRY_WAIT // 60} min")
                time.sleep(RESOLVE_RETRY_WAIT)
        if pending and len(pending) > max(2, 0.5 * len(google)):
            raise ResolutionThrottled(f"{len(pending)} of {len(google)} redirects undecoded "
                                      f"after {RESOLVE_RETRIES} retries")

    to_fetch = []
    for candidate in kept:
        url = candidate["url"]
        if "news.google.com" in url and _title_key(candidate["title"]) in direct_titles:
            continue  # counted above
        if "news.google.com" in url:
            counts["unresolved"] += 1  # not marked seen: retried next run
            log.write(stage="unresolved", url=url, title=candidate.get("title"))
            continue
        if is_vendor_or_wire(url):
            _mark_seen(seen_urls, candidate)
            counts["rejected"] += 1
            continue
        if canonical_url(url) in stored:
            _mark_seen(seen_urls, candidate)
            counts["duplicates"] += 1
            continue
        path = syndication_path(url)
        if path and path in stored_paths:
            _mark_seen(seen_urls, candidate)
            counts["duplicates"] += 1
            continue
        if url in seen_urls:  # resolved URL was seen under another Google id
            _mark_seen(seen_urls, candidate)
            counts["skipped_seen"] += 1
            continue
        to_fetch.append(candidate)
    if max_classify and len(to_fetch) > max_classify:
        print(f"Capping at {max_classify} this run; {len(to_fetch) - max_classify} deferred")
        to_fetch = to_fetch[:max_classify]
    print(f"Fetching and classifying {len(to_fetch)} with {CLASSIFY_WORKERS} workers\n")

    # Pass 4: fetch + classify + verify in parallel per chunk; dedupe and
    # append serially in submission order; checkpoint after each chunk.
    t0 = time.monotonic()
    done = 0
    with ThreadPoolExecutor(max_workers=CLASSIFY_WORKERS) as pool:
        for start in range(0, len(to_fetch), CHECKPOINT_EVERY):
            chunk = to_fetch[start:start + CHECKPOINT_EVERY]
            results = list(pool.map(lambda c: _fetch_and_classify(client, c), chunk))
            for candidate, res in zip(chunk, results):
                done += 1
                url = candidate["url"]
                print(f"[{done}/{len(to_fetch)}] {candidate['title'][:90]}")
                if "error" in res:
                    # Never mark seen on errors: retried next run.
                    print(f"  error, skipping (will retry next run): {res['error']}")
                    counts["errors"] += 1
                    continue
                _mark_seen(seen_urls, candidate)
                if res.get("no_text"):
                    # Paywall, 403, or no extractable body. A prior-record
                    # claim cannot be verified without the text.
                    counts["no_text"] += 1
                    log.write(stage="fetch", url=url, result="no_text")
                    print("  no article text; skipping")
                    continue
                cls, text = res["cls"], res["text"]
                if not cls or not cls.qualifies:
                    reason = cls.reason if cls else "no classification"
                    log.write(stage="classify", url=url, qualifies=False, reason=reason)
                    print(f"  rejected: {reason[:100]}")
                    counts["rejected"] += 1
                    continue
                problem = check_evidence(cls, text)
                if not problem and cls.state and cls.state.upper() not in US_STATES:
                    problem = f"state {cls.state!r} is not a US state"
                if cls.incident_date and cls.incident_date[:10] > _date_horizon():
                    # A misread date, not a reason to drop the story.
                    print(f"  future incident_date {cls.incident_date!r} blanked")
                    cls.incident_date = None
                if problem:
                    log.write(stage="verify", url=url, qualifies=False, reason=problem,
                              classification=cls.model_dump())
                    print(f"  rejected on verification: {problem}")
                    counts["rejected"] += 1
                    continue

                row = make_row(next_story_id(stories, id_base), cls, candidate)
                log.write(stage="classify", url=url, qualifies=True, row=row)
                try:
                    dup = check_duplicate(client, row, stories)
                except anthropic.APIError as e:
                    print(f"  dedupe call failed ({e}); treating as new")
                    dup = None

                if dup and dup.relation == "same_incident":
                    for s in stories:
                        if s["id"] == dup.matching_id:
                            urls = [u for u in s.get("additional_sources", "").split(" ") if u]
                            if url not in urls and url != s.get("source_url"):
                                urls.append(url)
                                s["additional_sources"] = " ".join(urls)
                            # Adopt a name or counts the first report lacked.
                            if not s.get("offender_name") and row["offender_name"]:
                                s["offender_name"] = row["offender_name"]
                                s["offender_key"] = row["offender_key"]
                            for f in ("prior_count_arrests", "prior_count_convictions",
                                      "prior_count_felony_convictions"):
                                if row[f] and (not s.get(f) or int(row[f]) > int(s[f])):
                                    s[f] = row[f]
                            if row["qualifies_strict"] == "yes":
                                s["qualifies_strict"] = "yes"
                    stored.add(canonical_url(url))
                    if path := syndication_path(url):
                        stored_paths.add(path)
                    log.write(stage="dedupe", url=url, relation="same_incident",
                              matching_id=dup.matching_id)
                    print(f"  duplicate of id {dup.matching_id}")
                    counts["duplicates"] += 1
                    continue

                if dup and dup.relation == "same_person_new_incident":
                    for s in stories:
                        if s["id"] == dup.matching_id and s.get("offender_key"):
                            row["offender_key"] = s["offender_key"]
                    log.write(stage="dedupe", url=url, relation="same_person_new_incident",
                              matching_id=dup.matching_id)
                    print(f"  same person as id {dup.matching_id}, new incident")
                    counts["same_person"] += 1

                stories.append(row)
                stored.add(canonical_url(url))
                if path := syndication_path(url):
                    stored_paths.add(path)
                strict = " STRICT" if row["qualifies_strict"] == "yes" else ""
                print(f"  ADDED{strict}: {row['offender_name'] or '(unnamed)'} | {row['city']}, "
                      f"{row['state']} | {row['new_offense_type']} | {row['release_status']}")
                counts["new"] += 1
            if checkpoint:
                checkpoint()
    if to_fetch:
        print(f"\nClassified {len(to_fetch)} in {int(time.monotonic() - t0)}s")

    return counts
