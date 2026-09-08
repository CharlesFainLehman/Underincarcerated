"""Remove published records without reusing their ids.

    python pipeline/remove_story.py --id 412 --id 1002151 --reason "wrong person; see issue #31"

Moves the rows from data/stories.csv or data/backfill/stories.csv to
data/removed.csv with a date and reason, then rebuilds the exports. The id
stays reserved forever and the site shows a tombstone at #id=N. Use this for
every correction that removes a row, so the record of what was published and
why it came down is kept.
"""

import argparse
from datetime import date

from build_exports import build_exports
from config import BACKFILL_STORIES_CSV, STORIES_CSV
from store import load_removed, load_stories, save_removed, save_stories


def remove_ids(ids: set[str], reason: str, rebuild: bool = True) -> list[dict]:
    removed = load_removed()
    already = {r["id"] for r in removed}
    moved = []
    for path in (STORIES_CSV, BACKFILL_STORIES_CSV):
        rows = load_stories(path)
        keep = []
        for r in rows:
            if r["id"] in ids and r["id"] not in already:
                r = dict(r, removed_date=date.today().isoformat(), removed_reason=reason)
                moved.append(r)
            else:
                keep.append(r)
        if len(keep) != len(rows):
            save_stories(keep, path)
    if moved:
        save_removed(removed + moved)
        if rebuild:
            build_exports()
    return moved


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--id", action="append", required=True, help="record id (repeatable)")
    ap.add_argument("--reason", required=True, help="why; shown publicly on the tombstone")
    ap.add_argument("--no-rebuild", action="store_true")
    a = ap.parse_args()
    moved = remove_ids(set(a.id), a.reason, rebuild=not a.no_rebuild)
    missing = set(a.id) - {r["id"] for r in moved}
    for r in moved:
        print(f"removed id {r['id']}: {r['offender_name']} | {r['city']}, {r['state']}")
    if missing:
        print(f"not found (or already removed): {', '.join(sorted(missing, key=int))}")


if __name__ == "__main__":
    main()
