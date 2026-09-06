"""Build the front-end data files in site/ from data/stories.csv.

Outputs:
  site/stories.csv, site/stories.json   one record per incident (daily + backfill)
  site/offenders.csv, site/offenders.json  one record per offender_key
  site/stats.json                        counts for headline figures and charts
  site/index.html is hand-maintained (the front end) and reads these files.

data/offenders.csv is also written (derived; never edit by hand).
"""

import csv
import json
import shutil
from collections import Counter, defaultdict
from datetime import date

from config import CSV_COLUMNS, OFFENDER_COLUMNS, OFFENDERS_CSV, SITE_DIR
from store import load_all_stories

INT_FIELDS = ("id", "age", "prior_count_arrests", "prior_count_convictions",
              "prior_count_felony_convictions")


def _max_int(values) -> str:
    ints = [int(v) for v in values if v]
    return str(max(ints)) if ints else ""


def build_offenders(stories: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for s in stories:
        if s.get("offender_key"):
            groups[s["offender_key"]].append(s)
    out = []
    for key, rows in groups.items():
        rows.sort(key=lambda r: r.get("incident_date") or "")
        dates = [r["incident_date"] for r in rows if r.get("incident_date")]
        # Longest printed name is usually the fullest.
        name = max((r["offender_name"] for r in rows), key=len)
        out.append({
            "offender_key": key,
            "offender_name": name,
            "state": rows[0]["state"],
            "story_ids": " ".join(r["id"] for r in rows),
            "incident_count": str(len(rows)),
            "first_incident_date": dates[0] if dates else "",
            "last_incident_date": dates[-1] if dates else "",
            "max_prior_arrests": _max_int(r["prior_count_arrests"] for r in rows),
            "max_prior_convictions": _max_int(r["prior_count_convictions"] for r in rows),
            "qualifies_strict": "yes" if any(r["qualifies_strict"] == "yes" for r in rows) else "no",
        })
    out.sort(key=lambda o: (-int(o["incident_count"]), o["offender_key"]))
    return out


def _to_json_record(row: dict) -> dict:
    rec = dict(row)
    for f in INT_FIELDS:
        if f in rec:
            rec[f] = int(rec[f]) if rec[f] else None
    if "additional_sources" in rec:
        rec["additional_sources"] = rec["additional_sources"].split()
    if "story_ids" in rec:
        rec["story_ids"] = [int(i) for i in rec["story_ids"].split()]
    if "incident_count" in rec:
        rec["incident_count"] = int(rec["incident_count"])
    for f in ("qualifies_strict",):
        rec[f] = rec.get(f) == "yes"
    return rec


def build_stats(stories: list[dict], offenders: list[dict]) -> dict:
    strict = [s for s in stories if s["qualifies_strict"] == "yes"]
    def by(field, rows):
        return dict(Counter(r[field] for r in rows if r.get(field)).most_common())
    return {
        "updated": date.today().isoformat(),
        "stories": len(stories),
        "stories_strict": len(strict),
        "offenders": len(offenders),
        "offenders_multiple_incidents": sum(1 for o in offenders if int(o["incident_count"]) > 1),
        "by_state": by("state", stories),
        "by_state_strict": by("state", strict),
        "by_release_status": by("release_status", stories),
        "by_offense_type": by("new_offense_type", stories),
        "by_severity": by("new_offense_severity", stories),
        "by_month": dict(sorted(Counter(s["incident_date"][:7] for s in stories
                                        if len(s.get("incident_date", "")) >= 7).items())),
    }


def build_exports() -> None:
    stories = load_all_stories()
    stories.sort(key=lambda r: (r.get("incident_date") or "", int(r["id"])))
    offenders = build_offenders(stories)
    SITE_DIR.mkdir(parents=True, exist_ok=True)

    OFFENDERS_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(OFFENDERS_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OFFENDER_COLUMNS)
        w.writeheader()
        w.writerows(offenders)

    with open(SITE_DIR / "stories.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(stories)
    shutil.copy(OFFENDERS_CSV, SITE_DIR / "offenders.csv")
    (SITE_DIR / "stories.json").write_text(
        json.dumps([_to_json_record(s) for s in stories], ensure_ascii=False), encoding="utf-8")
    (SITE_DIR / "offenders.json").write_text(
        json.dumps([_to_json_record(o) for o in offenders], ensure_ascii=False), encoding="utf-8")
    stats = build_stats(stories, offenders)
    (SITE_DIR / "stats.json").write_text(json.dumps(stats, indent=1), encoding="utf-8")
    print(f"Exports built: {len(stories)} stories, {len(offenders)} offenders")


if __name__ == "__main__":
    build_exports()
