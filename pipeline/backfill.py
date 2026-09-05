"""Historical backfill from GDELT.

Sweeps week-by-week from START_YEAR to the present, running each week's
candidates through the same triage/classify/dedupe path as the daily job.
Weekly windows because the repeat-offender queries saturate GDELT's
250-record cap on monthly windows.

Everything the backfill writes lives under data/backfill/ (stories, seen
URLs, done weeks) so it never touches the files the daily job commits; the
exports merge the two. Completed weeks are recorded, so an interrupted run
resumes at the first unfinished week when rerun with the same arguments.

GDELT throttles by IP. From a home or office IP one request every 8s is
tolerated; from GitHub Actions' shared IPs most calls 429. Run this
locally:

    export ANTHROPIC_API_KEY=...
    nohup caffeinate -i python -u pipeline/backfill.py --start 2017-01 --end 2017-12 \\
        --push-progress > backfill.log 2>&1 &
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import anthropic

import fetch
from build_exports import build_exports
from config import (BACKFILL_DONE_WEEKS_JSON, BACKFILL_ID_BASE, BACKFILL_QUERIES,
                    BACKFILL_SEEN_URLS_JSON, BACKFILL_STORIES_CSV, DECISIONS_DIR)
from fetch import GDELT_SLEEP, GDELT_STATS, QUERY_HITS, gdelt_search_split
from process import process_candidates
from progress import push_progress
from store import load_seen_urls, load_stories, save_seen_urls, save_stories

START_YEAR = 2017


def week_range(start: datetime, end: datetime):
    cur = start
    while cur <= end:
        nxt = cur + timedelta(days=7)
        yield cur, min(nxt - timedelta(seconds=1), end)
        cur = nxt


def load_done_weeks() -> set[str]:
    if not BACKFILL_DONE_WEEKS_JSON.exists():
        return set()
    return set(json.loads(BACKFILL_DONE_WEEKS_JSON.read_text(encoding="utf-8")))


def save_done_weeks(done: set[str]) -> None:
    BACKFILL_DONE_WEEKS_JSON.parent.mkdir(parents=True, exist_ok=True)
    BACKFILL_DONE_WEEKS_JSON.write_text(json.dumps(sorted(done), indent=0), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default=f"{START_YEAR}-01")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    parser.add_argument("--end", default=now.strftime("%Y-%m"))
    parser.add_argument("--push-progress", action="store_true",
                        help="commit+push data/backfill after each week")
    args = parser.parse_args()

    start = datetime.strptime(args.start, "%Y-%m")
    end_month = datetime.strptime(args.end, "%Y-%m")
    end = min(now, end_month.replace(day=28) + timedelta(days=4))

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set; refusing to run.")
    fetch.GDELT_PATIENT = True
    # Unbuffered: under nohup the log otherwise shows nothing for the
    # first several minutes and the run looks hung.
    sys.stdout.reconfigure(line_buffering=True)
    client = anthropic.Anthropic()
    stories = load_stories(BACKFILL_STORIES_CSV)
    seen = load_seen_urls(BACKFILL_SEEN_URLS_JSON)
    done = load_done_weeks()
    log = DECISIONS_DIR / f"backfill-{args.start}-{args.end}.jsonl"

    def checkpoint() -> None:
        save_stories(stories, BACKFILL_STORIES_CSV)
        save_seen_urls(seen, BACKFILL_SEEN_URLS_JSON)

    for w_start, w_end in week_range(start, end):
        label = w_start.strftime("%Y-%m-%d")
        if label in done:
            continue
        t0 = time.monotonic()
        stats_before = dict(GDELT_STATS)
        candidates: dict[str, dict] = {}
        print(f"\n--- week of {label}: querying GDELT ({len(BACKFILL_QUERIES)} queries, "
              f"8s apart, longer when throttled) ---")
        for q in BACKFILL_QUERIES:
            found = gdelt_search_split(q, w_start, w_end)
            for c in found:
                candidates.setdefault(c["url"], c)
            print(f"  GDELT {len(found):4} | {q[:70]}")
            time.sleep(GDELT_SLEEP)
        capped = [q for q, h in QUERY_HITS.items() if h["gdelt_capped"]]
        QUERY_HITS.clear()
        gave_up = GDELT_STATS["gave_up"] - stats_before["gave_up"]
        retries = GDELT_STATS["retries"] - stats_before["retries"]
        fresh = [c for c in candidates.values() if c["url"] not in seen]
        print(f"=== week of {label}: {len(candidates)} candidates, {len(fresh)} new, "
              f"{len(capped)} capped queries, gdelt {gave_up} gave-up/{retries} retries ===")

        counts = {"new": 0}
        if fresh:
            counts = process_candidates(client, fresh, stories, seen, decision_log=log,
                                        checkpoint=checkpoint, id_base=BACKFILL_ID_BASE)
        checkpoint()
        if gave_up == 0:
            # A week with a GDELT give-up is not done: rerun picks it up.
            done.add(label)
            save_done_weeks(done)
        elapsed = int(time.monotonic() - t0)
        summary = (f"week {label}: +{counts['new']} incidents (backfill total {len(stories)}), "
                   f"{len(fresh)} articles, {elapsed}s"
                   + ("" if gave_up == 0 else f", {gave_up} GDELT give-ups: week will be retried"))
        print(f"=== {summary} ===")
        if args.push_progress:
            push_progress(f"Backfill progress {summary}")

    build_exports()
    print(f"\nBackfill pass complete. Backfill file has {len(stories)} incidents; "
          f"{sum(1 for w, _ in week_range(start, end) if w.strftime('%Y-%m-%d') not in done)} "
          f"weeks still need a rerun.")


if __name__ == "__main__":
    main()
