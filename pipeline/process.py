"""Shared candidate-processing loop used by the daily run and the backfill.

Order per candidate:
  seen-URL check -> triage (batched, headline only) -> Google redirect
  resolve -> wire/blotter filter -> same-article URL check -> fetch ->
  classify -> evidence-quote check -> dedupe -> append.

Triage runs before URL resolution on purpose: decoding a Google News
redirect costs about a second of HTTP per URL, and the first live run spent
half an hour decoding 2,000 of them before classifying anything. Triage
needs only the headline, outlet, and snippet the feed already gave us.

Every decision is appended to a JSONL log (data/decisions/, gitignored, and
uploaded as a workflow artifact) so prompt calibration can be done against
real outputs instead of memory.
"""

import json
import re
from datetime import date
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


def default_decision_log() -> Path:
    return DECISIONS_DIR / f"{date.today().isoformat()}.jsonl"


def _mark_seen(seen: set[str], candidate: dict) -> None:
    seen.add(candidate["url"])
    if candidate.get("google_url"):
        seen.add(candidate["google_url"])


CHECKPOINT_EVERY = 25


def process_candidates(client: anthropic.Anthropic, candidates: list[dict],
                       stories: list[dict], seen_urls: set[str],
                       decision_log: Path | None = None,
                       checkpoint=None, max_classify: int = 0) -> dict:
    """Classify candidates and append qualifying, non-duplicate rows to stories.

    Mutates `stories` and `seen_urls` in place. Returns counts for logging.
    `checkpoint`, if given, is called every CHECKPOINT_EVERY fetched articles
    so a killed run (workflow timeout) keeps its progress. `max_classify`
    caps the number of articles fetched this run (0 = no cap); the rest are
    left unseen and picked up next run.
    """
    counts = {"new": 0, "duplicates": 0, "same_person": 0, "triaged_out": 0,
              "rejected": 0, "no_text": 0, "skipped_seen": 0, "errors": 0}
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

    # Pass 3: resolve Google redirects for kept candidates only, then the
    # cheap URL-level filters that need the real URL.
    to_fetch = []
    n_google = sum(1 for c in kept if "news.google.com" in c["url"])
    if n_google:
        print(f"Resolving {n_google} Google News redirects (about 1s each)...")
    for i, candidate in enumerate(kept, 1):
        if n_google and i % 100 == 0:
            print(f"  resolved {i}/{len(kept)}")
        resolve_candidate(candidate)
        url = candidate["url"]
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
    print(f"Fetching and classifying {len(to_fetch)}\n")

    # Pass 4: fetch, classify, verify, dedupe.
    for i, candidate in enumerate(to_fetch, 1):
        if checkpoint and i > 1 and (i - 1) % CHECKPOINT_EVERY == 0:
            checkpoint()  # after every CHECKPOINT_EVERY completed articles
        url = candidate["url"]
        print(f"[{i}/{len(to_fetch)}] {candidate['title'][:90]}")
        try:
            text = fetch_article_text(url)
            if not text:
                # Paywall, 403, or a page with no extractable body. A
                # prior-record claim cannot be verified without the text, so
                # there is nothing to classify. Mark seen: the same URL will
                # not fetch better tomorrow.
                _mark_seen(seen_urls, candidate)
                counts["no_text"] += 1
                log.write(stage="fetch", url=url, result="no_text")
                print("  no article text; skipping")
                continue
            cls = classify_article(client, candidate, text)
        except Exception as e:
            # Never mark seen on errors: the article must be retried next run.
            print(f"  error, skipping (will retry next run): {e}")
            counts["errors"] += 1
            continue

        _mark_seen(seen_urls, candidate)

        if not cls or not cls.qualifies:
            reason = cls.reason if cls else "no classification"
            log.write(stage="classify", url=url, qualifies=False, reason=reason)
            print(f"  rejected: {reason[:100]}")
            counts["rejected"] += 1
            continue

        problem = check_evidence(cls, text)
        if not problem and cls.state and cls.state.upper() not in US_STATES:
            problem = f"state {cls.state!r} is not a US state"
        if problem:
            log.write(stage="verify", url=url, qualifies=False, reason=problem,
                      classification=cls.model_dump())
            print(f"  rejected on verification: {problem}")
            counts["rejected"] += 1
            continue

        row = make_row(next_story_id(stories), cls, candidate)
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
            log.write(stage="dedupe", url=url, relation="same_incident", matching_id=dup.matching_id)
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

    return counts
