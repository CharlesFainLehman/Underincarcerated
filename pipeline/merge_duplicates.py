"""Merge rows that describe the same person and the same incident.

    python pipeline/merge_duplicates.py [--dry-run]

Until 2026-09-18 the daily run and the backfill each deduplicated only
against their own file, so an incident found by both was stored twice. This
finds same-incident pairs across both files with the deterministic rule the
pipeline uses at ingest (same offender key, incident dates within 30 days at
their common precision or one date missing, and the same city or the same
offense type), keeps the row published first, folds the other's sources,
counts, name, and photo into it, and moves the other to data/removed.csv
with the reason "duplicate: merged into record N" so its permalink says
where to look.
"""

import argparse
from collections import defaultdict
from datetime import date

from build_exports import build_exports
from config import BACKFILL_STORIES_CSV, STORIES_CSV
from remove_story import remove_ids
from store import load_stories, save_stories

WINDOW_DAYS = 30
COUNTS = ("prior_count_arrests", "prior_count_convictions", "prior_count_felony_convictions")


def _dates_close(a: str, b: str) -> bool | None:
    """True/False when both dates exist (compared at the coarser precision),
    None when either is missing."""
    a, b = (a or "").strip(), (b or "").strip()
    if not a or not b:
        return None
    n = min(len(a), len(b))
    if n >= 10:
        try:
            return abs((date.fromisoformat(a[:10]) - date.fromisoformat(b[:10])).days) <= WINDOW_DAYS
        except ValueError:
            return None
    if n >= 7:
        ya, ma, yb, mb = int(a[:4]), int(a[5:7]), int(b[:4]), int(b[5:7])
        return abs((ya * 12 + ma) - (yb * 12 + mb)) <= 1
    return a[:4] == b[:4]


def same_incident(a: dict, b: dict) -> bool:
    if not a.get("offender_key") or a["offender_key"] != b.get("offender_key"):
        return False
    close = _dates_close(a.get("incident_date"), b.get("incident_date"))
    if close is False:
        return False
    same_city = bool(a.get("city")) and a["city"].strip().lower() == (b.get("city") or "").strip().lower()
    same_offense = bool(a.get("new_offense_type")) and a["new_offense_type"] == b.get("new_offense_type")
    return same_city or same_offense


def merge_into(keep: dict, other: dict) -> None:
    urls = [u for u in (keep.get("additional_sources") or "").split() if u]
    for u in [other.get("source_url", "")] + (other.get("additional_sources") or "").split():
        if u and u != keep.get("source_url") and u not in urls:
            urls.append(u)
    keep["additional_sources"] = " ".join(urls)
    if not keep.get("offender_name") and other.get("offender_name"):
        keep["offender_name"], keep["offender_key"] = other["offender_name"], other["offender_key"]
    if not keep.get("age") and other.get("age"):
        keep["age"] = other["age"]
    for f in COUNTS:
        if other.get(f) and (not keep.get(f) or int(other[f]) > int(keep[f])):
            keep[f] = other[f]
    if other.get("qualifies_strict") == "yes":
        keep["qualifies_strict"] = "yes"
    if not keep.get("mugshot_url") and other.get("mugshot_url"):
        keep["mugshot_url"] = other["mugshot_url"]
    if not keep.get("prior_offenses") and other.get("prior_offenses"):
        keep["prior_offenses"] = other["prior_offenses"]
    if len(other.get("incident_date") or "") > len(keep.get("incident_date") or ""):
        keep["incident_date"] = other["incident_date"]   # the more precise date
    # A merged row has new sources; let the second-source search see it again.
    keep["corroboration_checked"] = ""


def find_merges(rows: list[dict]) -> list[tuple[dict, dict]]:
    """(keep, drop) pairs. Published-first wins: earlier date_added, then lower id."""
    order = lambda r: (r.get("date_added") or "", int(r["id"]))
    groups = defaultdict(list)
    for r in rows:
        if r.get("offender_key"):
            groups[r["offender_key"]].append(r)
    merges, dropped = [], set()
    for g in groups.values():
        g.sort(key=order)
        for i, keep in enumerate(g):
            if keep["id"] in dropped:
                continue
            for other in g[i + 1:]:
                if other["id"] not in dropped and same_incident(keep, other):
                    merges.append((keep, other))
                    dropped.add(other["id"])
    return merges


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    daily, backfill = load_stories(STORIES_CSV), load_stories(BACKFILL_STORIES_CSV)
    merges = find_merges(daily + backfill)
    for keep, other in merges:
        print(f"id {other['id']} -> id {keep['id']}: {keep['offender_name']} | "
              f"{keep.get('incident_date')} / {other.get('incident_date')} | "
              f"{keep.get('new_offense_type')} / {other.get('new_offense_type')}")
    print(f"\n{len(merges)} duplicate rows to merge")
    if a.dry_run or not merges:
        return
    by_id = {}
    for keep, other in merges:
        merge_into(keep, other)
        by_id[other["id"]] = keep["id"]
    save_stories(daily, STORIES_CSV)
    save_stories(backfill, BACKFILL_STORIES_CSV)
    for other_id, keep_id in by_id.items():
        remove_ids({other_id}, f"duplicate: merged into record {keep_id}", rebuild=False)
    build_exports()
    print(f"merged {len(merges)} rows")


if __name__ == "__main__":
    main()
