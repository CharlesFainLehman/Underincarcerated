"""Ledger of stories added per daily run (data/daily_adds.csv).

Bulk events (backfills, review sweeps) never touch it, so it isolates the
daily cadence from one-off database surgery.
"""

import csv
from datetime import date

from config import DATA_DIR

DAILY_ADDS_CSV = DATA_DIR / "daily_adds.csv"


def record_run(new_count: int) -> None:
    rows = []
    if DAILY_ADDS_CSV.exists():
        with open(DAILY_ADDS_CSV, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    today = date.today().isoformat()
    for r in rows:
        if r["date"] == today:
            r["added"] = str(int(r["added"]) + new_count)
            break
    else:
        rows.append({"date": today, "added": str(new_count)})
    DAILY_ADDS_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(DAILY_ADDS_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["date", "added"])
        w.writeheader()
        w.writerows(rows)
