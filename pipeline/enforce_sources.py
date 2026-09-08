"""Remove stories that stay single-sourced after the second-source search.

    python pipeline/enforce_sources.py [--dry-run]

Publication rule: a story needs two or more distinct outlets, or a primary
source on TRUSTED_OUTLETS. A row that has been through the second-source
search (corroboration_checked set) and still fails the rule is moved to
data/removed.csv with a policy reason. Rows the search has not reached yet
are left in place; build_exports keeps them off the site until it has.
"""

import argparse

from config import BACKFILL_STORIES_CSV, REQUIRE_CORROBORATION, STORIES_CSV
from corroborate import pending, publishable
from remove_story import remove_ids
from store import load_stories

REASON = "policy: reported by one outlet only, and that outlet is not on the trusted list"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if not REQUIRE_CORROBORATION:
        print("REQUIRE_CORROBORATION is off; nothing to do")
        return
    drop, wait = set(), 0
    for path in (STORIES_CSV, BACKFILL_STORIES_CSV):
        for r in load_stories(path):
            if publishable(r):
                continue
            if pending(r):
                wait += 1
            else:
                drop.add(r["id"])
    print(f"{len(drop)} rows single-sourced after the search, {wait} still awaiting it")
    if drop and not a.dry_run:
        moved = remove_ids(drop, REASON, rebuild=False)
        print(f"removed {len(moved)}")


if __name__ == "__main__":
    main()
