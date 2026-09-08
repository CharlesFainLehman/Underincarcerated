"""Read/write helpers for the stories CSV, the seen-URL cache, and offender keys."""

import csv
import json
import re
import unicodedata
from datetime import date

from classify import qualifies_strict
from pathlib import Path

from config import (BACKFILL_STORIES_CSV, CSV_COLUMNS, REMOVED_COLUMNS, REMOVED_CSV,
                    SEEN_URLS_JSON, STORIES_CSV)

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def offender_key(name: str | None, state: str | None) -> str:
    """Normalized person key: last name, first name, state. Middle names,
    initials, suffixes, punctuation, and accents are dropped so "John M.
    Smith Jr." and "John Smith" collide. Dedupe confirms the match; this
    only nominates candidates. Empty if there is no name."""
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z\s]", " ", s.lower())
    parts = [p for p in s.split() if p not in _SUFFIXES]
    if len(parts) < 2:
        return ""
    return f"{parts[-1]}_{parts[0]}_{(state or '').upper()}"


def load_stories(path: Path | None = None) -> list[dict]:
    path = path or STORIES_CSV
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_all_stories() -> list[dict]:
    """Daily stories plus backfill stories: what the exports publish."""
    return load_stories(STORIES_CSV) + load_stories(BACKFILL_STORIES_CSV)


def save_stories(stories: list[dict], path: Path | None = None) -> None:
    path = path or STORIES_CSV
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(stories)


def load_removed() -> list[dict]:
    if not REMOVED_CSV.exists():
        return []
    with open(REMOVED_CSV, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_removed(rows: list[dict]) -> None:
    REMOVED_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(REMOVED_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS + REMOVED_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def reserved_ids(base: int = 0, span: int = 1_000_000) -> set[int]:
    """Ids of removed rows in this id range. Never reused: a permalink to a
    removed record must not resolve to a different person."""
    return {int(r["id"]) for r in load_removed() if base <= int(r["id"]) < base + span}


def next_story_id(stories: list[dict], base: int = 0, reserved: set[int] | None = None) -> int:
    if reserved is None:
        reserved = reserved_ids(base)
    return max(max((int(s["id"]) for s in stories), default=base), max(reserved, default=base)) + 1


def load_seen_urls(path: Path | None = None) -> set[str]:
    path = path or SEEN_URLS_JSON
    if not path.exists():
        return set()
    with open(path, encoding="utf-8") as f:
        return set(json.load(f))


def save_seen_urls(urls: set[str], path: Path | None = None) -> None:
    path = path or SEEN_URLS_JSON
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted(urls), f, indent=0)


def _int_str(v) -> str:
    return "" if v is None else str(int(v))


def make_row(story_id: int, cls, candidate: dict) -> dict:
    """Build a CSV row from a classification result and its source candidate."""
    strict = qualifies_strict(cls.prior_count_arrests, cls.prior_count_convictions,
                              cls.prior_count_felony_convictions)
    return {
        "id": str(story_id),
        "date_added": date.today().isoformat(),
        "incident_date": cls.incident_date or "",
        "city": cls.city or "",
        "state": (cls.state or "").upper(),
        "offender_name": cls.offender_name or "",
        "offender_key": offender_key(cls.offender_name, cls.state),
        "age": _int_str(cls.age),
        "new_offense_type": cls.new_offense_type or "",
        "new_offense_severity": cls.new_offense_severity or "",
        "prior_count_arrests": _int_str(cls.prior_count_arrests),
        "prior_count_convictions": _int_str(cls.prior_count_convictions),
        "prior_count_felony_convictions": _int_str(cls.prior_count_felony_convictions),
        "prior_offenses": cls.prior_offenses or "",
        "prior_evidence_quote": cls.prior_evidence_quote or "",
        "release_status": cls.release_status,
        "release_evidence_quote": cls.release_evidence_quote or "",
        "releasing_jurisdiction": cls.releasing_jurisdiction or "",
        "outcome": cls.outcome or "",
        "summary": cls.summary or "",
        "qualifies_strict": "yes" if strict else "no",
        "mugshot_url": "",
        "mugshot_checked": "",
        "source_name": candidate.get("source", ""),
        "source_url": candidate["url"],
        "additional_sources": "",
        "confidence": cls.confidence,
    }
