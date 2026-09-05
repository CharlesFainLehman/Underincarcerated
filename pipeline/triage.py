"""Stage 1: cheap headline/snippet triage before any article fetch.

Discovery returns hundreds of candidates a day, most of them policy pieces,
court dockets, and stories where "prior convictions" is incidental. Fetching
and fully classifying every one is the slow part of the run, so a batched
Haiku call first asks, for 25 headlines at a time, which are plausibly a
specific person arrested for a new crime with a documented record.

The triage errs toward keeping: the full classifier decides precision.
Anything the model does not return a decision for is kept (fail open).
"""

import anthropic
from pydantic import BaseModel

from config import TRIAGE_BATCH_SIZE, TRIAGE_MODEL


class TriageDecision(BaseModel):
    index: int
    worth_fetching: bool


class TriageBatch(BaseModel):
    decisions: list[TriageDecision]


SYSTEM_PROMPT = """You screen news headlines for a database of stories about repeat offenders: specific, named people arrested, charged, or convicted for a NEW crime who had a prior criminal record, or who were free at the time because of a release decision (bail, bond, probation, parole, pretrial release, early release, dropped charges).

For each numbered headline, decide whether the full article is WORTH FETCHING: could it plausibly describe such a person and incident? Keep anything plausible. Only reject when the headline makes clear the article is:
- a policy, statistics, editorial, or opinion piece with no specific incident
- about legislation, a court ruling on procedure, a budget, or an election
- a police blotter, arrest log, or list of unrelated bookings
- about a crime outside the United States
- entertainment, sports, or a fictional work

Return one decision per index. Do not skip any index."""


def triage_candidates(client: anthropic.Anthropic, candidates: list[dict]) -> list[bool]:
    """One bool per candidate, in order. True means fetch and classify."""
    keep = [True] * len(candidates)
    for start in range(0, len(candidates), TRIAGE_BATCH_SIZE):
        batch = candidates[start:start + TRIAGE_BATCH_SIZE]
        lines = []
        for i, c in enumerate(batch):
            snippet = (c.get("snippet") or "").strip()
            line = f"{i}. [{c.get('source', '')}] {c.get('title', '')}"
            if snippet:
                line += f" -- {snippet[:200]}"
            lines.append(line)
        response = client.messages.parse(
            model=TRIAGE_MODEL,
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": "\n".join(lines)}],
            output_format=TriageBatch,
        )
        parsed = response.parsed_output
        if not parsed:
            continue
        for d in parsed.decisions:
            if 0 <= d.index < len(batch):
                keep[start + d.index] = d.worth_fetching
    return keep
