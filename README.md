# Underincarcerated

A daily-updated database of news stories about repeat offenders: people arrested, charged,
or convicted for a new crime in the United States who had a documented prior record, or who
were free at the time because of a release decision (bail, bond, probation, parole, pretrial
release, early release, dropped charges).

The backend follows [flock-crime-tracker](https://github.com/CharlesFainLehman/flock-crime-tracker).
The front end is not built yet; the pipeline publishes `site/stories.json`, `site/offenders.json`,
and `site/stats.json` for it.

## How it works

1. **Discovery.** Each day, candidate articles are pulled from the
   [GDELT DOC 2.0 API](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/) and Google News
   RSS using ~30 narrow queries such as `"repeat offender" arrested` and `"out on bond" arrested`
   ([pipeline/fetch.py](pipeline/fetch.py), [pipeline/config.py](pipeline/config.py)).
2. **Triage.** Headlines are screened in batches of 25 by Claude Haiku before any article is
   fetched. Policy pieces, blotters, and non-US stories are dropped here; anything plausible
   is kept ([pipeline/triage.py](pipeline/triage.py)).
3. **Classification.** Each surviving article's text is extracted and classified by Haiku as
   qualifying or not, with structured fields: offender, new offense, prior counts, release
   status, and two verbatim evidence quotes ([pipeline/classify.py](pipeline/classify.py)).
4. **Verification.** A row is rejected unless both evidence quotes appear verbatim in the
   fetched article text. This is the guard against the model inventing or inflating a record.
5. **Deduplication.** New rows are compared against rows for the same person and rows in the
   same state within 21 days. Same incident: the URL is attached to the existing row. Same
   person, new incident: a new row sharing the offender key ([pipeline/dedupe.py](pipeline/dedupe.py)).
6. **Publishing.** The database is [data/stories.csv](data/stories.csv). `data/offenders.csv`
   and everything in `site/` are derived from it by
   [pipeline/build_exports.py](pipeline/build_exports.py) and deployed to GitHub Pages.

The daily job is [.github/workflows/daily.yml](.github/workflows/daily.yml). Its commit
history is an audit log of what was added each day. Every triage, classify, verify, and
dedupe decision is written to a JSONL log uploaded as a workflow artifact.

## Inclusion criteria

A record must describe a **specific, identifiable person** arrested, charged, or convicted for
a **new offense in the United States**, and the article must state at least one of:

1. one or more prior convictions or arrests, with some specificity (a count, a named prior
   offense, a prior sentence, or a prior prison term);
2. that the person was on pretrial release, bail, bond, probation, parole, supervised release,
   or in a diversion program at the time of the new offense;
3. that the person had been released early, had charges dropped or reduced, or had been denied
   detention in a prior case that the article connects to the new offense.

Every qualifying story is stored. The **strict** flag (`qualifies_strict`) marks the subset
with 5+ prior arrests, or 5+ prior convictions, or 3+ prior felony convictions, as stated in
the article. Counts are never estimated: "more than a dozen" is 13, "numerous" is blank.

**Excluded by rule:**
- policy, statistics, editorial, and opinion pieces with no specific new incident
- "repeat offender" or "known to police" used as a label with no concrete prior
- priors that are only traffic infractions, or a juvenile record with no detail
- re-arrest on the same case (failure to appear, bond violation, retrial)
- crimes outside the United States
- police blotters, arrest logs, mugshot aggregators, and wire/press-release sources
- wrongful-arrest and exoneration stories

## Schema

One row per new-offense incident in `data/stories.csv`:

| Column | Notes |
|---|---|
| `id`, `date_added` | |
| `incident_date` | new offense; YYYY-MM-DD, YYYY-MM, or YYYY |
| `city`, `state` | state always set when known |
| `offender_name`, `offender_key` | name as printed; key is `last_first_ST`, links rows for one person |
| `age` | |
| `new_offense_type`, `new_offense_severity` | controlled vocabularies in `config.py` |
| `prior_count_arrests`, `prior_count_convictions`, `prior_count_felony_convictions` | only when the article states a number |
| `prior_offenses` | named priors, semicolon-separated |
| `prior_evidence_quote` | verbatim sentence from the article; verified |
| `release_status` | controlled vocabulary; `none stated` if the article is silent |
| `release_evidence_quote` | verbatim; verified; required when status is not `none stated` |
| `releasing_jurisdiction` | |
| `outcome`, `summary` | |
| `qualifies_strict` | yes/no, from the counts |
| `source_name`, `source_url`, `additional_sources`, `confidence` | |

`data/offenders.csv` has one row per `offender_key`: name, state, story ids, incident count,
first and last incident date, max prior counts, strict flag. Never edit it by hand.

## Caveats

- Entries reflect claims made in news reports, which typically rely on police and prosecutor
  statements. Inclusion is not independent verification of the record.
- Coverage is limited to English-language US outlets indexed by GDELT and Google News.
- Classification is automated. Corrections can be made by editing `data/stories.csv` directly;
  `validate_data.py` runs before every commit.

## Running locally

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...
python pipeline/run_daily.py                    # one daily update (DAYS_BACK=3 for a wider net)
python pipeline/backfill.py --start 2024-01 --end 2024-03
python pipeline/build_exports.py               # rebuild site/ from the CSV only
python pipeline/validate_data.py
python -m pytest -q                            # offline tests, no API key needed
```

Decision logs land in `data/decisions/` (gitignored).
