"""Shared configuration for the repeat-offender story pipeline."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
SITE_DIR = REPO_ROOT / "site"

STORIES_CSV = DATA_DIR / "stories.csv"
OFFENDERS_CSV = DATA_DIR / "offenders.csv"
SEEN_URLS_JSON = DATA_DIR / "seen_urls.json"
QUERY_STATS_JSON = DATA_DIR / "query_stats.json"
DECISIONS_DIR = DATA_DIR / "decisions"  # per-run audit log, committed gzipped

# The backfill writes to its own files so a multi-day local backfill and the
# daily Actions job never edit the same CSV (both appending to stories.csv
# would conflict on every push and collide on ids). build_exports merges
# them. Backfill ids start at BACKFILL_ID_BASE.
BACKFILL_DIR = DATA_DIR / "backfill"
BACKFILL_STORIES_CSV = BACKFILL_DIR / "stories.csv"
BACKFILL_SEEN_URLS_JSON = BACKFILL_DIR / "seen_urls.json"
BACKFILL_DONE_WEEKS_JSON = BACKFILL_DIR / "done_weeks.json"
BACKFILL_ID_BASE = 1_000_000

# Haiku for the bulk stages (triage, classify, dedupe): volume is hundreds of
# articles a day and the tasks are extraction, not judgment. Sonnet for the
# low-volume feedback triage.
TRIAGE_MODEL = "claude-haiku-4-5"
CLASSIFY_MODEL = "claude-haiku-4-5"
DEDUPE_MODEL = "claude-haiku-4-5"
REVIEW_MODEL = "claude-sonnet-5"

TRIAGE_BATCH_SIZE = 25

# Discovery queries.
#
# GDELT rate-limits hard: the first live run (29 sequential queries, 3-day
# window) spent 88 minutes in discovery and 13 queries gave up after 429
# backoffs. So GDELT gets a few OR-grouped queries (its syntax allows
# ("a" OR "b") term), and fetch.py splits the window in half whenever a query
# hits the 250-record cap. Google News RSS is cheap, so it keeps the narrow
# list: each narrow query returns up to 100 results, which is more coverage
# than one broad one.
GDELT_QUERIES = [
    '("repeat offender" OR "career criminal" OR "habitual offender" OR "prolific offender") arrested',
    '("prior convictions" OR "previously convicted" OR "felony convictions" OR "prior felony") arrested',
    '("lengthy criminal history" OR "extensive criminal history" OR "long criminal history" OR "prior arrests") arrested',
    '("out on bail" OR "out on bond" OR "released on bond" OR "free on bond" OR "posted bond") arrested',
    '("on parole" OR "on probation" OR "supervised release" OR "pretrial release") arrested',
    '("released without bail" OR "cashless bail" OR "no bail" OR "released early" OR "charges dropped") arrested',
]

GOOGLE_NEWS_QUERIES = [
    # Prior record
    '"repeat offender" arrested',
    '"repeat offender" charged',
    '"career criminal" arrested',
    '"prior convictions" arrested',
    '"prior convictions" charged',
    '"lengthy criminal history"',
    '"extensive criminal history"',
    '"long criminal history" arrested',
    '"habitual offender" charged',
    '"prior arrests" charged',
    '"previously convicted" arrested',
    '"prolific offender"',
    '"felony convictions" arrested',
    '"criminal record" arrested again',
    # Release status at time of offense
    '"out on bail" arrested',
    '"out on bond" arrested',
    '"released on bond" arrested',
    '"free on bond" arrested',
    '"on parole" arrested',
    '"on probation" arrested',
    '"pretrial release" arrested',
    '"supervised release" arrested',
    '"released without bail" arrested',
    '"cashless bail" arrested',
    '"no bail" arrested again',
    '"released early" arrested',
    '"charges dropped" arrested again',
    # Counts
    '"arrested" "times before"',
    '"arrests" "criminal history" charged',
]

# Backfill uses the GDELT groups only (Google News RSS has no date filter).
BACKFILL_QUERIES = GDELT_QUERIES

# Threshold for the strict "serious repeat offender" flag. The database stores
# every story with at least one concrete prior; this flag marks the subset the
# front end shows by default. Any one condition is sufficient.
STRICT_MIN_ARRESTS = 5
STRICT_MIN_CONVICTIONS = 5
STRICT_MIN_FELONY_CONVICTIONS = 3

CSV_COLUMNS = [
    "id",
    "date_added",
    "incident_date",
    "city",
    "state",
    "offender_name",
    "offender_key",
    "age",
    "new_offense_type",
    "new_offense_severity",
    "prior_count_arrests",
    "prior_count_convictions",
    "prior_count_felony_convictions",
    "prior_offenses",
    "prior_evidence_quote",
    "release_status",
    "release_evidence_quote",
    "releasing_jurisdiction",
    "outcome",
    "summary",
    "qualifies_strict",
    "source_name",
    "source_url",
    "additional_sources",
    "confidence",
]

OFFENDER_COLUMNS = [
    "offender_key",
    "offender_name",
    "state",
    "story_ids",
    "incident_count",
    "first_incident_date",
    "last_incident_date",
    "max_prior_arrests",
    "max_prior_convictions",
    "qualifies_strict",
]

NEW_OFFENSE_TYPES = [
    "homicide",
    "shooting",
    "sexual assault",
    "robbery",
    "assault",
    "carjacking",
    "kidnapping",
    "burglary",
    "theft/larceny",
    "vehicle theft",
    "drug offense",
    "weapons offense",
    "DUI/vehicular",
    "arson",
    "domestic violence",
    "child abuse",
    "fraud",
    "other",
]

OFFENSE_SEVERITIES = ["violent", "property", "drug", "weapons", "other"]

RELEASE_STATUSES = [
    "pretrial release",
    "bail/bond",
    "probation",
    "parole",
    "supervised release",
    "early release",
    "charges dropped",
    "diversion",
    "none stated",
]

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN",
    "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV",
    "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN",
    "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC", "PR", "GU", "VI", "AS", "MP",
}
