"""Daily pipeline: discover -> triage -> classify -> verify -> dedupe -> save -> export."""

import os
import sys

import anthropic

from build_exports import build_exports
from daily_adds import record_run
from fetch import discover_daily, save_query_stats
from process import default_decision_log, process_candidates
from store import load_seen_urls, load_stories, save_seen_urls, save_stories


def main() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set; refusing to run.")
    client = anthropic.Anthropic()
    stories = load_stories()
    seen = load_seen_urls()

    print("Discovering candidates...")
    candidates = discover_daily(days_back=int(os.environ.get("DAYS_BACK", "1")))
    save_query_stats()
    print(f"Found {len(candidates)} candidate URLs "
          f"({sum(1 for c in candidates if c['url'] in seen)} already seen)\n")

    def checkpoint() -> None:
        save_stories(stories)
        save_seen_urls(seen)

    counts = process_candidates(client, candidates, stories, seen,
                                decision_log=default_decision_log(),
                                checkpoint=checkpoint,
                                max_classify=int(os.environ.get("MAX_CLASSIFY", "0")))

    save_stories(stories)
    save_seen_urls(seen)
    record_run(counts["new"])
    build_exports()

    print(f"\nDone. New: {counts['new']} (of which same-person: {counts['same_person']}), "
          f"duplicates: {counts['duplicates']}, triaged out: {counts['triaged_out']}, "
          f"rejected: {counts['rejected']}, no text: {counts['no_text']}, "
          f"unresolved: {counts['unresolved']}, "
          f"already seen: {counts['skipped_seen']}, errors: {counts['errors']}. "
          f"Database now has {len(stories)} incidents.")

    processed = counts["new"] + counts["duplicates"] + counts["rejected"] + counts["errors"]
    if processed and counts["errors"] > processed / 2:
        sys.exit("More than half of processed articles errored; failing the run.")


if __name__ == "__main__":
    main()
