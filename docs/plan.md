# Underincarcerated: repeat-offender news database — plan

A daily-updated database of news stories in which a person arrested or charged for a new
crime had a documented prior record, or was free at the time of the offense because of a
release decision (bail, bond, probation, parole, early release, dropped charges, diversion).

The backend copies the architecture of
[flock-crime-tracker](https://github.com/CharlesFainLehman/flock-crime-tracker):
discover -> extract -> classify -> dedupe -> store -> validate -> commit. The front end is
separate and out of scope here; the pipeline publishes clean CSV and JSON for it.

## 1. What is different from the Flock tracker

The Flock pipeline keys on a brand name. "Repeat offender" is a concept, not a keyword.
This changes four things.

| Problem | Flock tracker | This project |
|---|---|---|
| Discovery precision | High: "Flock" is rare | Low: phrases like "prior convictions" appear in thousands of stories a day |
| Volume | ~5-20 candidates/day | Est. 500-2,000 candidates/day; GDELT's 250-record cap saturates on broad queries |
| Unit of record | One incident | One incident, but the same person can recur across months (arrest -> trial -> new arrest). Person recurrence is itself a finding |
| Extraction risk | Camera role is usually one sentence | Prior-record claims are the whole point and the easiest thing for a model to invent or inflate |

Consequences:
- Many narrow queries and one-day windows, not six queries over three days.
- Two-stage classification: cheap headline/snippet triage before full fetch, then full-text extraction. Full fetch of 1,500 articles/day is the slow part, not the model cost.
- The classifier must return a verbatim `evidence_quote` for the prior record and for the release status. The pipeline rejects any row whose quote does not appear in the fetched text. This is the main hallucination guard.
- An offender key (normalized name + state) links rows for the same person across stories.

## 2. Inclusion criteria (draft; needs your sign-off)

A record must describe a **specific, named or clearly identified individual** who was
**arrested, charged, or convicted for a new offense**, AND the article states at least one of:

1. Prior conviction(s) or prior arrest(s), with some specificity (a count, a named prior offense, a prior sentence, "lengthy criminal history" plus at least one concrete prior).
2. The person was on pretrial release, bail, bond, probation, parole, supervised release, or a diversion program at the time of the new offense.
3. The person had been released early, had charges dropped or reduced, or had been denied detention in a prior case that the article links to the new offense.

**Excluded by rule:**
- Policy, statistics, or opinion pieces on recidivism, bail reform, or sentencing with no specific new incident.
- Stories where "repeat offender" or "known to police" appears without any concrete prior.
- Prior traffic infractions only.
- Juvenile records when the article gives no offense detail.
- Stories where the only prior is the same incident (e.g., re-arrest after failing to appear on the same charge). Flag as `same_case_only` and exclude.
- Vendor, wire, and aggregator sources (same list as Flock, plus police-blotter aggregators).
- Non-US incidents (default; configurable).

Open decisions for you (section 8) affect the exact wording.

## 3. Schema

`data/stories.csv`, one row per new-offense incident.

| Column | Notes |
|---|---|
| `id` | sequential |
| `date_added` | |
| `incident_date` | new offense date; YYYY-MM-DD, YYYY-MM, or YYYY |
| `city`, `state` | state required, two-letter |
| `offender_name` | as printed in the article; blank if unnamed (see decision 8a) |
| `offender_key` | normalized `lastname_firstname_ST`; used for person linking |
| `age` | integer or blank |
| `new_offense_type` | enum: homicide, shooting, sexual assault, robbery, assault, carjacking, burglary, theft/larceny, vehicle theft, drug offense, weapons offense, DUI/vehicular, arson, domestic violence, child abuse, fraud, other |
| `new_offense_severity` | enum: violent, property, drug, weapons, other |
| `prior_count_arrests` | integer if stated, else blank |
| `prior_count_convictions` | integer if stated, else blank |
| `prior_count_felony_convictions` | integer if stated, else blank |
| `prior_offenses` | short free text: "burglary; assault; two drug felonies" |
| `prior_evidence_quote` | verbatim sentence from the article; validated against fetched text |
| `release_status` | enum: pretrial release, bail/bond, probation, parole, supervised release, early release, charges dropped, diversion, none stated |
| `release_evidence_quote` | verbatim; validated; blank if `none stated` |
| `releasing_jurisdiction` | county/state/court if named |
| `outcome` | short phrase: arrested, charged, convicted, sentenced, killed by police, at large |
| `summary` | 1-2 factual sentences |
| `source_name`, `source_url`, `additional_sources` | as Flock |
| `confidence` | high/medium/low |
| `qualifies_strict` | yes/no; computed from the counts (section 8b) |

`data/offenders.csv` is derived, not edited: one row per `offender_key` with story ids, first/last seen, incident count. Rebuilt by `build_exports.py`.

## 4. Pipeline modules (`pipeline/`)

Copied from flock-crime-tracker with changes noted.

| Module | Change from Flock |
|---|---|
| `config.py` | ~25 queries (section 5); new enums; new CSV columns |
| `fetch.py` | Same GDELT + Google News code. Window is one day. Add per-query result cap check: log when a query hits 250 so we know to narrow it |
| `triage.py` | **New.** Haiku on headline + snippet only. Returns `worth_fetching: bool`. Goal: drop ~70% of candidates before any HTTP fetch |
| `classify.py` | New prompt and Pydantic model with the schema above. Post-parse check: both `_evidence_quote` fields must appear in the article text after whitespace normalization, else reject with reason `quote_not_found` |
| `dedupe.py` | Candidate set = same `offender_key`, or same state within 21 days. Model decides same incident vs. same person/new incident vs. unrelated. Same person/new incident is a new row that shares the key |
| `process.py` | Same loop plus triage stage and quote validation |
| `store.py` | Same, new columns |
| `validate_data.py` | Same checks plus: enum vocabularies, quote fields non-empty when status is stated, no future dates |
| `build_exports.py` | Replaces `build_site.py`. Writes `site/stories.csv`, `site/stories.json`, `site/offenders.csv`, `site/offenders.json`, `site/stats.json`, and a placeholder `site/index.html` |
| `run_daily.py`, `backfill.py`, `daily_adds.py`, `review_feedback.py` | Same, renamed strings |

Model: `claude-haiku-4-5` for triage, classify, dedupe. `claude-sonnet-5` for feedback triage and review sweeps. Verify current ids and pricing against the API docs at build time.

## 5. Discovery queries (draft)

Grouped; each runs against GDELT and Google News.

- Prior record: `"repeat offender" arrested`, `"career criminal" arrested`, `"prior convictions" arrested`, `"lengthy criminal history"`, `"extensive criminal history"`, `"habitual offender" charged`, `"prior arrests" charged`, `"previously convicted" arrested`, `"prolific offender"`
- Release status: `"out on bail" arrested`, `"out on bond" arrested`, `"released on bond" arrested again`, `"on parole" arrested`, `"on probation" arrested`, `"pretrial release" arrested`, `"supervised release" arrested`, `"released without bail"`, `"cashless bail" arrested`
- Counts: `"arrested" "times before"`, `"felony convictions" arrested`

Expect to prune and add after the first week of logs. Each query's hit rate is recorded in `data/query_stats.json`.

## 6. Cost and runtime estimate

Per day, assuming 1,500 raw candidates and 60% already seen:

| Stage | Items | Approx. cost |
|---|---|---|
| Triage (Haiku, ~300 tokens) | 600 | $0.10 |
| Fetch | 180 | 0 (time: ~10 min) |
| Classify (Haiku, ~4k tokens) | 180 | $0.40 |
| Dedupe | ~60 | $0.05 |

Roughly $0.50-1.00/day, 20-30 minutes wall time. Well inside Actions free minutes.

Backfill to 2017 is the expensive step: GDELT's cap means weekly windows and narrow queries, roughly 9 years x 52 weeks x 20 queries = ~9,400 GDELT calls at 5s each = 13 hours of fetch alone, plus classification of maybe 40,000 articles at ~$0.003 = ~$120. Run in monthly chunks via the manual backfill workflow.

## 7. Phases

1. **Scaffold** (this repo, `pipeline/`, `data/`, `.github/workflows/`). Port modules. Write prompts. Add `requirements.txt`, `README.md`, issue templates. No workflow secrets yet.
2. **Calibration run.** Run discovery for one day locally. Save every triage and classify decision to `data/calibration/`. Hand-review ~100 decisions. Tune the prompts and the exclusion list. Repeat until precision on a fresh sample is above ~90% and the quote check rejects fewer than ~10% of accepted rows.
3. **Automate.** `daily.yml` at 10:30 UTC with `ANTHROPIC_API_KEY` secret. Validation gate before commit. `deploy.yml` builds exports on data changes.
4. **Adversarial review.** After two weeks of daily data, run the review protocol from the Flock repo (false positives, duplicates, field accuracy, source spot-check, embarrassment factors). Fix the prompt for each failure class found.
5. **Backfill.** 2024 first, then earlier years in monthly chunks. Review again after each year.
6. **Front end hand-off.** Freeze the export format. Document it in `docs/exports.md`.

## 8. Decisions (settled 2026-09-05)

a. **Names.** Stored as printed.
b. **Threshold.** Ingest every story with at least one concrete prior or a release-status
   condition. A `qualifies_strict` flag marks 5+ prior arrests, or 5+ prior convictions, or 3+
   prior felony convictions, as stated in the article. The front end filters on the flag.
c. **Scope.** US only. Non-US stories rejected at triage and again at classification.
d. **Backfill depth.** 2017.
e. **Person linking.** `offenders.csv` built from phase 1.
f. **Repo layout.** Everything in this repo. Pipeline in `pipeline/`, data in `data/`, front
   end and exports in `site/`.

## 9. Status

Phase 1 scaffold is done. The first live run (2026-09-05, 3-day window) timed out before
classifying anything and taught two things, both fixed:

- GDELT rate-limits 29 sequential queries into the ground (88 minutes of discovery, 13 queries
  gave up). GDELT now gets 6 OR-grouped queries with window splitting on cap; Google News
  keeps the narrow list.
- Decoding Google News redirects costs ~1s per URL and ran before triage. Triage now runs
  first on the feed's own headline and snippet; only kept candidates are decoded.
- Runs checkpoint every 25 articles, the commit step runs on timeout, and `MAX_CLASSIFY`
  bounds a calibration run.

The first run found 2,579 candidate URLs over 3 days, roughly 860/day, in line with the
estimate in section 6.

Wall-clock design after the second run (which also ran long): everything network-bound is
parallel. Redirect decoding runs on 6 threads, fetch + classify + verify on 8; only dedupe
and append are serial, per chunk of 25, with a checkpoint after each chunk. Google News
items whose headline matches a direct-URL candidate are dropped before decoding. Fetch
timeout is 15s. Each stage prints its elapsed time.

## 10. Calibration run 1 (2026-09-05, 3-day window, 200 articles)

Timings: discovery 6.5 min (5 wasted on GDELT), triage 2 min, redirect decoding 10 min,
classification 5 min. 2,500 candidates; triage kept 75%; of 200 fetched, 42 had no text,
31 rejected, 129 classified qualifying, 26 merged as duplicates, 103 stored, 32 strict.

Row-level precision on the 103 rows, by hand review: about 93%. Errors found and fixed in
the prompt or code:

- **Bail confusion (systematic).** Bail set on the *new* charge was recorded as "out on
  bail/bond" in about a quarter of bail/bond rows. Prompt now defines release status as
  the status at the time of the new offense, from an earlier case.
- **Label-only priors.** "Armed career criminal" charge, "several convictions," "repeat
  offender" with nothing concrete: three rows. Prompt tightened; "Nth offense" charges are
  the one accepted label, counted as N-1 priors.
- **Not a new crime.** Failure-to-appear warrant arrest; ICE arrest after release with no
  new offense: two rows. Prompt now excludes both.
- **Several arrestees in one row.** Three rows. Prompt and code enforce one person per row.
- **Dedupe misses.** A third syndicated copy and a video page with no state each got
  their own row. Same person plus dates within 21 days now merges deterministically;
  name-only matching when a state is missing.
- **Video pages.** Minimum article text raised to 500 chars; /video/ paths skipped.

Open policy question: juveniles. Two rows were 14- and 15-year-olds with detailed prior
arrests. The criteria admit them when the article gives offense detail.

GDELT is now off for the daily run (47 of 2,600 candidates over three runs, at 5 minutes
of 429 backoff per run). The backfill still needs it and must run from a non-Actions IP.

Calibration data was reset and the same window re-run on the fixed prompt.

## 11. Calibration run 2 (same window, fixed prompt)

14 minutes. 2,438 candidates; of 200 fetched, 59 had no text, 29 rejected by the classifier,
6 rejected by the quote check, 105 classified qualifying, 26 merged, 79 stored, 31 strict.

Hand review of the 79 rows: one label-only false positive ("several convictions and
suspended sentences"), no multi-name rows, no non-crime "new offenses", no duplicate
people except one legitimately linked pair. Release status still misread in about 3 of 11
bail/bond rows, down from about 8 of 31, and all three rows qualify on their priors anyway.
Row-level precision about 97%.

Cost of the tighter prompt: two articles with named prior offenses were rejected as
"labels" (Caleb Jenkins, Austin: three dismissed gun charges; Larry Barnes, Aurora: prior
drug distribution, assault on an officer, burglary). Prompt now says a named prior offense
is concrete without a count.

Next: run the full 3-day window uncapped to seed the database, then let the 10:30 UTC cron
take over with a 1-day window.
