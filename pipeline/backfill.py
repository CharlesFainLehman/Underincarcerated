"""Historical backfill from GDELT.

Sweeps week-by-week from START_YEAR to the present, running each week's
candidates through the same triage/classify/dedupe path as the daily job.
Weekly windows because the repeat-offender queries saturate GDELT's
250-record cap on monthly windows. Progress is checkpointed after every
week via seen_urls.json, so the script is safe to interrupt and rerun.

Usage:
    python backfill.py [--start 2017-01] [--end 2026-08] [--push-progress]
"""

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import anthropic

from build_exports import build_exports
from config import BACKFILL_QUERIES, DECISIONS_DIR
from fetch import GDELT_STATS, QUERY_HITS, gdelt_search
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default=f"{START_YEAR}-01")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    parser.add_argument("--end", default=now.strftime("%Y-%m"))
    parser.add_argument("--push-progress", action="store_true",
                        help="commit+push data/ after each week (CI only)")
    args = parser.parse_args()

    start = datetime.strptime(args.start, "%Y-%m")
    end_month = datetime.strptime(args.end, "%Y-%m")
    end = min(now, end_month.replace(day=28) + timedelta(days=4))

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set; refusing to run.")
    client = anthropic.Anthropic()
    stories = load_stories()
    seen = load_seen_urls()
    log = DECISIONS_DIR / f"backfill-{args.start}-{args.end}.jsonl"

    for w_start, w_end in week_range(start, end):
        label = w_start.strftime("%Y-%m-%d")
        t0 = time.monotonic()
        stats_before = dict(GDELT_STATS)
        candidates: dict[str, dict] = {}
        for q in BACKFILL_QUERIES:
            for c in gdelt_search(q, w_start, w_end):
                candidates.setdefault(c["url"], c)
            time.sleep(6)
        capped = [q for q, h in QUERY_HITS.items() if h["gdelt_capped"]]
        QUERY_HITS.clear()
        fresh = [c for c in candidates.values() if c["url"] not in seen]
        print(f"\n=== week of {label}: {len(candidates)} candidates, {len(fresh)} new, "
              f"{len(capped)} capped queries ===")

        counts = {"new": 0}
        if fresh:
            counts = process_candidates(client, fresh, stories, seen, decision_log=log,
                                        checkpoint=lambda: (save_stories(stories),
                                                            save_seen_urls(seen)))
            save_stories(stories)
            save_seen_urls(seen)

        gave_up = GDELT_STATS["gave_up"] - stats_before["gave_up"]
        retries = GDELT_STATS["retries"] - stats_before["retries"]
        elapsed = int(time.monotonic() - t0)
        summary = (f"week {label}: +{counts['new']} incidents (total {len(stories)}), "
                   f"{len(fresh)} articles, gdelt {gave_up} gave-up/{retries} retries, "
                   f"{elapsed}s")
        print(f"=== {summary} ===")
        if args.push_progress:
            push_progress(f"Backfill progress {summary}")

    build_exports()
    print(f"\nBackfill complete. Database has {len(stories)} incidents.")


if __name__ == "__main__":
    main()
