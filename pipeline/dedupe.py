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


def _name_part(key: str) -> str:
    """offender_key without its state suffix, so a report that omits the
    state still matches the same person."""
    return key.rsplit("_", 1)[0] if key else ""


def same_person(a: dict, b: dict) -> bool:
    ka, kb = a.get("offender_key", ""), b.get("offender_key", "")
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    return _name_part(ka) == _name_part(kb) and (not a.get("state") or not b.get("state"))


def obvious_same_incident(new_row: dict, s: dict) -> bool:
    """Same person, incident dates within the window (or one missing), and the
    same city or the same offense type: merged without asking the model. The
    model missed a third syndicated copy of one arrest story in calibration;
    this is cheaper and stricter. Two people who share a common name in the
    same state within three weeks still go to the model, which sees the full
    rows."""
    if not same_person(new_row, s):
        return False
    a, b = _parse_date(new_row.get("incident_date", "")), _parse_date(s.get("incident_date", ""))
    if a and b and abs((a - b).days) > DATE_WINDOW_DAYS:
        return False
    same_city = bool(new_row.get("city")) and (new_row.get("city") or "").strip().lower() == (s.get("city") or "").strip().lower()
    same_offense = bool(new_row.get("new_offense_type")) and new_row.get("new_offense_type") == s.get("new_offense_type")
    return same_city or same_offense


def ages_consistent(a: dict, b: dict) -> bool:
    """False only when both rows give an age and the ages cannot belong to one
    person given the gap between incidents (a year of slack for birthdays and
    rounding). Used before linking two incidents to one offender: a wrong link
    gives someone another person's record."""
    try:
        age_a, age_b = int(a.get("age") or 0), int(b.get("age") or 0)
    except ValueError:
        return True
    if not age_a or not age_b:
        return True
    da, db = _parse_date(a.get("incident_date", "")), _parse_date(b.get("incident_date", ""))
    if not da or not db:
        return abs(age_a - age_b) <= 2
    years = abs((da - db).days) / 365.25
    return abs(abs(age_a - age_b) - years) <= 1.5


def find_candidates(new_row: dict, stories: list[dict]) -> list[dict]:
    new_date = _parse_date(new_row.get("incident_date", ""))
    key = new_row.get("offender_key", "")
    out = []
    for s in stories:
        if same_person(new_row, s):
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
        same_person_rank = 0 if same_person(new_row, s) else 1
        added = _parse_date(s.get("date_added", ""))
        recency = -(added.toordinal() if added else 0)
        old_date = _parse_date(s.get("incident_date", ""))
        if not new_date or not old_date:
            return (same_person_rank, 1, 0, recency)
        return (same_person_rank, 0, abs((new_date - old_date).days), recency)
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
    for s in candidates:
        if obvious_same_incident(new_row, s):
            return DedupeResult(relation="same_incident", matching_id=s["id"])

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
