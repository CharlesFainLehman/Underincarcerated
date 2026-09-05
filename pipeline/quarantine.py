"""Move story rows that fail validation into data/quarantine.csv.

Run by the workflows when validate_data.py fails, so one bad row (a model
misreading a date as next week) can never throw away a 45-minute run. The
quarantine file keeps the row and the reason for hand review; the story
can be fixed and moved back.
"""

import csv
from datetime import date

from config import BACKFILL_STORIES_CSV, CSV_COLUMNS, DATA_DIR, STORIES_CSV
from store import load_stories, save_stories
from validate_data import row_problems

QUARANTINE_CSV = DATA_DIR / "quarantine.csv"


def main() -> None:
    moved = 0
    for path in (STORIES_CSV, BACKFILL_STORIES_CSV):
        rows = load_stories(path)
        keep, bad = [], []
        for r in rows:
            problems = row_problems(r)
            if problems:
                bad.append(dict(r, quarantined=date.today().isoformat(),
                                reason="; ".join(problems)))
            else:
                keep.append(r)
        if not bad:
            continue
        save_stories(keep, path)
        exists = QUARANTINE_CSV.exists()
        with open(QUARANTINE_CSV, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_COLUMNS + ["quarantined", "reason"],
                               extrasaction="ignore")
            if not exists:
                w.writeheader()
            w.writerows(bad)
        for r in bad:
            print(f"quarantined id {r['id']}: {r['reason']}")
        moved += len(bad)
    print(f"quarantined {moved} rows")


if __name__ == "__main__":
    main()
