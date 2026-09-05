"""Read/write helpers for the stories CSV, the seen-URL cache, and offender keys."""

import csv
import json
import re
import unicodedata
from datetime import date

from classify import qualifies_strict
from config import CSV_COLUMNS, SEEN_URLS_JSON, STORIES_CSV

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


def load_stories() -> list[dict]:
    if not STORIES_CSV.exists():
        return []
    with open(STORIES_CSV, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_stories(stories: list[dict]) -> None:
    STORIES_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(STORIES_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(stories)


def next_story_id(stories: list[dict]) -> int:
    return max((int(s["id"]) for s in stories), default=0) + 1


def load_seen_urls() -> set[str]:
    if not SEEN_URLS_JSON.exists():
        return set()
    with open(SEEN_URLS_JSON, encoding="utf-8") as f:
        return set(json.load(f))


def save_seen_urls(urls: set[str]) -> None:
    SEEN_URLS_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(SEEN_URLS_JSON, "w", encoding="utf-8") as f:
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
        "source_name": candidate.get("source", ""),
        "source_url": candidate["url"],
        "additional_sources": "",
        "confidence": cls.confidence,
    }
