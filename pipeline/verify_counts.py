"""Re-check stored prior counts against the cited article text.

    python pipeline/verify_counts.py            # report only
    python pipeline/verify_counts.py --apply    # blank unverifiable counts

For every stored row, fetch the source article and require each non-blank
prior count to appear in the text (digits, number word, or ordinal; n or
n+1 for "Nth offense" charges), the same rule new rows face at ingest. A
count the article never prints is blanked. Rows that then fall below the
strict threshold are moved to data/removed.csv with the reason recorded.
Articles that cannot be fetched are left as they are and listed.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor

from classify import count_in_text, qualifies_strict
from config import BACKFILL_STORIES_CSV, STORIES_CSV
from fetch import fetch_article_text
from remove_story import remove_ids
from store import load_stories, save_stories

FIELDS = ("prior_count_arrests", "prior_count_convictions", "prior_count_felony_convictions")


def check_row(row: dict) -> tuple[str, list[str]]:
    """('ok' | 'no_text' | 'bad', [fields not stated])."""
    if not any(row.get(f) for f in FIELDS):
        return "ok", []
    text = fetch_article_text(row["source_url"])
    if not text:
        return "no_text", []
    bad = [f for f in FIELDS if row.get(f) and not count_in_text(int(row[f]), text)]
    return ("bad" if bad else "ok"), bad


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()

    totals = {"ok": 0, "no_text": 0, "bad": 0}
    to_remove: set[str] = set()
    for path in (STORIES_CSV, BACKFILL_STORIES_CSV):
        rows = load_stories(path)
        with ThreadPoolExecutor(max_workers=a.workers) as pool:
            results = list(pool.map(check_row, rows))
        changed = False
        for row, (status, bad) in zip(rows, results):
            totals[status] += 1
            if status != "bad":
                continue
            print(f"id {row['id']}: {row['offender_name']} | " +
                  ", ".join(f"{f}={row[f]}" for f in bad) + " not in article")
            if a.apply:
                for f in bad:
                    row[f] = ""
                row["qualifies_strict"] = "yes" if qualifies_strict(
                    *(int(row[f]) if row[f] else None for f in FIELDS)) else "no"
                changed = True
                if row["qualifies_strict"] != "yes":
                    to_remove.add(row["id"])
        if changed:
            save_stories(rows, path)
    print(f"\nverified {totals['ok']}, unverifiable counts {totals['bad']}, no text {totals['no_text']}")
    if a.apply and to_remove:
        moved = remove_ids(to_remove, "prior count not stated in the cited article; below threshold without it")
        print(f"removed {len(moved)} rows that no longer meet the strict threshold")


if __name__ == "__main__":
    main()
