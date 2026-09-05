"""Validate data/ integrity before any workflow commit.

Exits non-zero on: unparseable JSON, git conflict markers, duplicate ids,
future dates, values outside the enum vocabularies, non-US states, missing
evidence quotes, a strict flag that disagrees with the counts, or a wire /
blotter primary source. Corruption can never be committed.
"""

import csv
import json
import re
import sys
from datetime import date, timedelta

from classify import qualifies_strict
from config import (DATA_DIR, NEW_OFFENSE_TYPES, OFFENSE_SEVERITIES, RELEASE_STATUSES,
                    STORIES_CSV, US_STATES)

MARKERS = ("<<<<<<<", "=======", ">>>>>>>")
BAD_SOURCE = re.compile(r"prnewswire|businesswire|globenewswire|einpresswire|press_release|"
                        r"press-release|arrests\.org|mugshots\.com|bustednewspaper", re.I)


def fail(msg: str) -> None:
    sys.exit(f"DATA VALIDATION FAILED: {msg}")


def _int(v: str) -> int | None:
    return int(v) if v else None


def main() -> None:
    for p in DATA_DIR.glob("*.json"):
        try:
            json.load(open(p, encoding="utf-8"))
        except json.JSONDecodeError as e:
            fail(f"{p.name} is not valid JSON: {e}")

    for p in DATA_DIR.glob("*.csv"):
        text = p.read_text(encoding="utf-8")
        for m in MARKERS:
            if any(line.startswith(m) for line in text.splitlines()):
                fail(f"{p.name} contains git conflict marker {m!r}")
        rows = list(csv.DictReader(open(p, newline="", encoding="utf-8")))
        ids = [r["id"] for r in rows if r.get("id")]
        if len(ids) != len(set(ids)):
            dupes = sorted({i for i in ids if ids.count(i) > 1})[:5]
            fail(f"{p.name} has duplicate ids: {dupes}")

    if not STORIES_CSV.exists():
        print("data validation OK (no stories yet)")
        return
    rows = list(csv.DictReader(open(STORIES_CSV, newline="", encoding="utf-8")))
    horizon = (date.today() + timedelta(days=2)).isoformat()
    for r in rows:
        rid = r.get("id")
        v = r.get("incident_date") or ""
        if v and v[:10] > horizon:
            fail(f"id {rid}: incident_date {v!r} is in the future")
        if v and v[:4] < "2000":
            fail(f"id {rid}: incident_date {v!r} is implausibly old")
        if r.get("state") and r["state"] not in US_STATES:
            fail(f"id {rid}: state {r['state']!r} is not a US state")
        if r.get("new_offense_type") and r["new_offense_type"] not in NEW_OFFENSE_TYPES:
            fail(f"id {rid}: new_offense_type {r['new_offense_type']!r} outside vocabulary")
        if r.get("new_offense_severity") and r["new_offense_severity"] not in OFFENSE_SEVERITIES:
            fail(f"id {rid}: new_offense_severity {r['new_offense_severity']!r} outside vocabulary")
        if r.get("release_status") not in RELEASE_STATUSES:
            fail(f"id {rid}: release_status {r.get('release_status')!r} outside vocabulary")
        if r["release_status"] != "none stated" and not r.get("release_evidence_quote"):
            fail(f"id {rid}: release_status set without release_evidence_quote")
        if not r.get("prior_evidence_quote") and r["release_status"] == "none stated":
            fail(f"id {rid}: no prior_evidence_quote and no release status")
        try:
            strict = qualifies_strict(_int(r["prior_count_arrests"]),
                                      _int(r["prior_count_convictions"]),
                                      _int(r["prior_count_felony_convictions"]))
        except ValueError:
            fail(f"id {rid}: non-integer prior count")
        if (r.get("qualifies_strict") == "yes") != strict:
            fail(f"id {rid}: qualifies_strict {r.get('qualifies_strict')!r} disagrees with counts")
        if BAD_SOURCE.search(r.get("source_url", "")):
            fail(f"id {rid}: wire/blotter primary source; re-source or remove")

    print(f"data validation OK ({len(rows)} stories)")


if __name__ == "__main__":
    main()
