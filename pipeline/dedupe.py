"""Incident- and person-level deduplication.

Multiple outlets cover the same arrest, and the same person recurs across
months (arrest, then trial, then a new arrest). Before adding a row, compare
it against existing rows for the same offender key (any date) and the same
state within a date window, and ask Haiku which of three things it is:

  same_incident            -> attach the URL to the existing row; no new row
  same_person_new_incident -> new row that shares the existing offender_key
  unrelated                -> new row

Follow-up coverage of one case (arrest -> charges -> plea -> sentencing) is
the same incident.
"""

from datetime import date, datetime
from typing import Literal, Optional

import anthropic
from pydantic import BaseModel

from config import DEDUPE_MODEL

DATE_WINDOW_DAYS = 21
MAX_CANDIDATES = 20


class DedupeResult(BaseModel):
    relation: Literal["same_incident", "same_person_new_incident", "unrelated"]
    matching_id: Optional[str] = None


def _parse_date(value: str) -> date | None:
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(value or "", fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def find_candidates(new_row: dict, stories: list[dict]) -> list[dict]:
    new_date = _parse_date(new_row.get("incident_date", ""))
    key = new_row.get("offender_key", "")
    out = []
    for s in stories:
        if key and s.get("offender_key") == key:
            out.append(s)
            continue
        if not new_row.get("state") or s.get("state") != new_row.get("state"):
            continue
        old_date = _parse_date(s.get("incident_date", ""))
        if new_date and old_date and abs((new_date - old_date).days) > DATE_WINDOW_DAYS:
            continue
        out.append(s)

    # Rank: same person first, then by date proximity, then most recently
    # added. Ties toward recency matter when the new story lacks a date: nine
    # syndicated copies of one undated story must each find their just-added
    # twin at the end of the list.
    def _rank(s: dict):
        same_person = 0 if (key and s.get("offender_key") == key) else 1
        added = _parse_date(s.get("date_added", ""))
        recency = -(added.toordinal() if added else 0)
        old_date = _parse_date(s.get("incident_date", ""))
        if not new_date or not old_date:
            return (same_person, 1, 0, recency)
        return (same_person, 0, abs((new_date - old_date).days), recency)
    out.sort(key=_rank)
    return out[:MAX_CANDIDATES]


def _describe(s: dict) -> str:
    return (f"{s.get('incident_date', '')} | {s.get('city', '')}, {s.get('state', '')} | "
            f"{s.get('offender_name') or '(unnamed)'} | {s.get('new_offense_type', '')} | "
            f"{s.get('summary', '')}")


def check_duplicate(client: anthropic.Anthropic, new_row: dict,
                    stories: list[dict]) -> DedupeResult:
    candidates = find_candidates(new_row, stories)
    if not candidates:
        return DedupeResult(relation="unrelated")

    existing = "\n".join(f"- id {s['id']}: {_describe(s)}" for s in candidates)
    response = client.messages.parse(
        model=DEDUPE_MODEL,
        max_tokens=256,
        system=("You deduplicate a database of news stories about repeat offenders. "
                "Decide how the new entry relates to the existing entries.\n"
                "- same_incident: the same person and the same new offense. Follow-up "
                "coverage of one case (arrest, then charges, then plea or sentencing) is "
                "the same incident. Different outlets often report slightly different "
                "dates and charges for one event.\n"
                "- same_person_new_incident: clearly the same person, but a different "
                "new offense on a different occasion.\n"
                "- unrelated: a different person, or you cannot tell.\n"
                "Same name in the same state is strong evidence of the same person. "
                "If unsure between same_incident and unrelated, choose unrelated."),
        messages=[{
            "role": "user",
            "content": f"Existing entries:\n{existing}\n\nNew entry:\n{_describe(new_row)}",
        }],
        output_format=DedupeResult,
    )
    result = response.parsed_output
    known = {s["id"] for s in candidates}
    if result.relation != "unrelated" and result.matching_id not in known:
        return DedupeResult(relation="unrelated")
    return result
