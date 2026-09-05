"""Stage 2: full-text classification and extraction with Claude Haiku.

Decides whether an article describes a specific person arrested, charged, or
convicted for a new offense who has a documented prior record or was on
release at the time, and extracts the structured fields for the database.

The model answers with a JSON object in plain text, which pydantic then
validates. The first live run used the API's structured-output mode
(messages.parse) and every call stalled for three minutes and failed with
"Schema is too complex": this schema has two large enums and many optional
fields, and the grammar compiler gave up. Plain JSON has no such limit, and
Haiku follows a schema in the prompt reliably; unknown enum values are
coerced to the catch-all rather than failing the row.

The two *_evidence_quote fields are the accuracy guard. The model must copy
the article's own sentence for the prior record and for the release status;
process.py rejects any row whose quote is not actually in the fetched text.
A prior-record claim the model cannot quote is a prior-record claim it
invented or inflated.
"""

import json
import re
from typing import Literal, Optional

import anthropic
from pydantic import BaseModel, ValidationError, field_validator

from config import (CLASSIFY_MODEL, NEW_OFFENSE_TYPES, OFFENSE_SEVERITIES, RELEASE_STATUSES,
                    STRICT_MIN_ARRESTS, STRICT_MIN_CONVICTIONS, STRICT_MIN_FELONY_CONVICTIONS)

NewOffenseType = Literal[tuple(NEW_OFFENSE_TYPES)]  # type: ignore[valid-type]
Severity = Literal[tuple(OFFENSE_SEVERITIES)]  # type: ignore[valid-type]
ReleaseStatus = Literal[tuple(RELEASE_STATUSES)]  # type: ignore[valid-type]


class StoryClassification(BaseModel):
    qualifies: bool
    reason: str
    incident_date: Optional[str] = None  # YYYY-MM-DD, YYYY-MM, or YYYY
    city: Optional[str] = None
    state: Optional[str] = None  # two-letter US postal code
    offender_name: Optional[str] = None
    age: Optional[int] = None
    new_offense_type: Optional[NewOffenseType] = None
    new_offense_severity: Optional[Severity] = None
    prior_count_arrests: Optional[int] = None
    prior_count_convictions: Optional[int] = None
    prior_count_felony_convictions: Optional[int] = None
    prior_offenses: Optional[str] = None
    prior_evidence_quote: Optional[str] = None
    release_status: ReleaseStatus = "none stated"
    release_evidence_quote: Optional[str] = None
    releasing_jurisdiction: Optional[str] = None
    outcome: Optional[str] = None
    summary: Optional[str] = None
    confidence: Literal["high", "medium", "low"] = "low"

    @field_validator("new_offense_type", mode="before")
    @classmethod
    def _coerce_offense(cls, v):
        return v if v in NEW_OFFENSE_TYPES or v is None else "other"

    @field_validator("new_offense_severity", mode="before")
    @classmethod
    def _coerce_severity(cls, v):
        return v if v in OFFENSE_SEVERITIES or v is None else "other"

    @field_validator("release_status", mode="before")
    @classmethod
    def _coerce_release(cls, v):
        return v if v in RELEASE_STATUSES else "none stated"

    @field_validator("confidence", mode="before")
    @classmethod
    def _coerce_confidence(cls, v):
        return v if v in ("high", "medium", "low") else "low"

    @field_validator("age", "prior_count_arrests", "prior_count_convictions",
                     "prior_count_felony_convictions", mode="before")
    @classmethod
    def _coerce_int(cls, v):
        if v is None or v == "" or isinstance(v, bool):
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    @field_validator("state", mode="before")
    @classmethod
    def _coerce_state(cls, v):
        return v.strip().upper() if isinstance(v, str) and v.strip() else None


class ClassificationParseError(ValueError):
    """The model's reply was not a JSON object matching StoryClassification."""


def parse_classification(text: str) -> StoryClassification:
    """Extract the first JSON object from a model reply and validate it."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ClassificationParseError(f"no JSON object in reply: {text[:200]!r}")
    try:
        return StoryClassification.model_validate(json.loads(text[start:end + 1]))
    except (json.JSONDecodeError, ValidationError) as e:
        raise ClassificationParseError(str(e)[:300]) from e


_SCHEMA_JSON = json.dumps(StoryClassification.model_json_schema(), separators=(",", ":"))


SYSTEM_PROMPT = f"""You classify news articles for a public database of repeat offenders: specific people arrested, charged, or convicted for a NEW crime in the United States who had a documented prior criminal record, or who were free at the time of the new crime because of a release decision.

An article QUALIFIES only if ALL of the following hold:
1. It describes a specific, identifiable person (normally named) and a specific new offense for which that person was arrested, charged, or convicted.
2. The article itself states at least one of:
   a. one or more prior convictions or prior arrests, with some specificity: a number, a named prior offense, a prior sentence, or a prior prison term. "Lengthy criminal history" alone is not enough; "lengthy criminal history including two robbery convictions" is.
   b. the person was on pretrial release, bail, bond, probation, parole, supervised release, or in a diversion program at the time of the new offense.
   c. the person had been released early, had charges dropped or reduced, or had been denied detention in a prior case, and the article connects that to the new offense.
3. The new offense occurred in the United States.

An article does NOT qualify if it is:
- A policy, statistics, editorial, or opinion piece on recidivism, bail, or sentencing with no specific new incident.
- A story where "repeat offender", "known to police", or "criminal history" appears with no concrete prior.
- A story whose only priors are traffic infractions or a juvenile record with no offense detail.
- A story where the only "prior" is the same case: re-arrest for failure to appear, a bond violation on the same charge, or a retrial.
- A story about a crime outside the United States.
- A police blotter, arrest log, or list of unrelated bookings.
- A story about a wrongful arrest or exoneration.

Field guidance for qualifying articles:
- incident_date: date of the NEW offense, as specific as the article allows (YYYY-MM-DD, YYYY-MM, or YYYY). Use the arrest date if the offense date is not given. Use the publication date only if nothing better is stated.
- state: two-letter US postal code. Always provide it when the article names or implies any location; leave city blank rather than state.
- offender_name: full name as printed. Blank if the article does not name the person.
- age: as stated, else blank.
- new_offense_type: best fit from: {", ".join(NEW_OFFENSE_TYPES)}. Use the most serious charge.
- new_offense_severity: one of {", ".join(OFFENSE_SEVERITIES)}.
- prior_count_arrests, prior_count_convictions, prior_count_felony_convictions: ONLY if the article states a number. "More than a dozen arrests" is 13. "Dozens" or "numerous" is blank. Never estimate.
- prior_offenses: short list of the prior offenses the article names, separated by semicolons. Blank if none named.
- prior_evidence_quote: copy VERBATIM, character for character, the single sentence from the article that best documents the prior record. Do not paraphrase, shorten, or fix typos. If the article has no such sentence, the story does not qualify under 2a.
- release_status: one of {", ".join(RELEASE_STATUSES)}. "bail/bond" means released on bail or bond in an earlier, different case. "none stated" if the article does not say.
- release_evidence_quote: copy VERBATIM the sentence that documents the release status. Required whenever release_status is not "none stated".
- releasing_jurisdiction: the county, city, state, or court that released the person, if named.
- outcome: short phrase for the new case: "arrested", "charged", "convicted", "sentenced", "killed by police", "at large".
- summary: 1-2 factual sentences: who, what new offense, and what the prior record or release status was.
- confidence: "high" if the article is explicit on both the new offense and the prior record or release; "medium" if reasonably clear; "low" if you are inferring.

Always give a one-sentence reason for your decision.

Reply with ONLY a JSON object, no prose and no code fence, matching this JSON schema exactly (use null for unknown optional fields):
{_SCHEMA_JSON}"""


def classify_article(client: anthropic.Anthropic, candidate: dict,
                     article_text: str) -> StoryClassification:
    user_msg = (
        f"Headline: {candidate.get('title')}\n"
        f"Outlet: {candidate.get('source')}\n"
        f"Published: {candidate.get('published')}\n\n"
        f"Full article text:\n\n{article_text}"
    )
    response = client.messages.create(
        model=CLASSIFY_MODEL,
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
    return parse_classification(text)


_QUOTE_MAP = str.maketrans({
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", " ": " ",
})


def normalize_for_match(text: str) -> str:
    """Lowercase, straight quotes, collapsed whitespace. Enough to survive
    trafilatura's cleanup and a model that straightens curly quotes; not
    enough to let a paraphrase through."""
    text = (text or "").translate(_QUOTE_MAP).lower()
    text = re.sub(r"[^\w\s'\"$%.,;:!?()/-]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def quote_in_text(quote: str | None, text: str) -> bool:
    """True if the quote appears verbatim (after normalization) in the text.
    A quote under 20 characters is too short to be evidence of anything."""
    q = normalize_for_match(quote or "").strip("\"' ")
    if len(q) < 20:
        return False
    return q in normalize_for_match(text)


def check_evidence(cls: StoryClassification, text: str) -> str | None:
    """Return a rejection reason if the extraction's evidence does not hold
    up against the article text, else None."""
    has_prior = bool(cls.prior_evidence_quote)
    has_release = cls.release_status != "none stated"
    if not has_prior and not has_release:
        return "no prior record and no release status"
    if has_prior and not quote_in_text(cls.prior_evidence_quote, text):
        return "prior_evidence_quote not found in article"
    if has_release:
        if not cls.release_evidence_quote:
            return "release_status set without a quote"
        if not quote_in_text(cls.release_evidence_quote, text):
            return "release_evidence_quote not found in article"
    return None


def qualifies_strict(arrests: int | None, convictions: int | None,
                     felony_convictions: int | None) -> bool:
    """The 'serious repeat offender' flag: 5+ prior arrests, or 5+ prior
    convictions, or 3+ prior felony convictions. Any one is sufficient."""
    return ((arrests or 0) >= STRICT_MIN_ARRESTS
            or (convictions or 0) >= STRICT_MIN_CONVICTIONS
            or (felony_convictions or 0) >= STRICT_MIN_FELONY_CONVICTIONS)
