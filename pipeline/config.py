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
# Since 2026-09-08 only strict stories are kept. Non-strict stories are still
# classified (the counts are only known after reading the article) but are
# dropped before storage; the decision log records them.
STRICT_ONLY = True

# Rows removed after publication (corrections, policy removals) live here with
# a date and reason. Their ids are never reused, so a shared permalink to a
# removed record never resolves to a different person.
REMOVED_CSV = DATA_DIR / "removed.csv"
REMOVED_COLUMNS = ["removed_date", "removed_reason"]

# Only people the article says were arrested, charged, indicted, arraigned,
# convicted, or sentenced are stored. Suspects who are "wanted", "at large",
# or "killed by police" are excluded: identification is weakest there.
STORED_OUTCOME_RE = (r"arrest|charg|indict|arraign|convict|sentenc|plead|guilty|booked|jailed|detain|"
                     r"held|custody|accused|apprehend|extradit|bond|bail|recognizance|bound over|"
                     r"stand trial|incompetent|pending (trial|hearing)")
# No one under 18 is stored, whatever the article gives.
MIN_AGE = 18

# Outlets with professional newsrooms and legal review. A story whose primary
# source is one of these is not sent through the second-source search; a
# subdomain matches too (e.g. abcnews.go.com). Edit freely.
TRUSTED_OUTLETS = (
    # national
    "nytimes.com", "washingtonpost.com", "wsj.com", "usatoday.com", "nypost.com",
    "nydailynews.com", "cbsnews.com", "nbcnews.com", "abcnews.go.com", "foxnews.com",
    "cnn.com", "npr.org", "pbs.org", "reuters.com", "bloomberg.com", "politico.com",
    "axios.com", "propublica.org", "themarshallproject.org",
    # metro dailies
    "latimes.com", "chicagotribune.com", "suntimes.com", "bostonglobe.com", "bostonherald.com",
    "inquirer.com", "baltimoresun.com", "washingtontimes.com", "newsday.com", "courant.com",
    "providencejournal.com", "ajc.com", "miamiherald.com", "tampabay.com", "orlandosentinel.com",
    "sun-sentinel.com", "jacksonville.com", "tennessean.com", "courier-journal.com", "nola.com",
    "theadvocate.com", "dallasnews.com", "star-telegram.com", "houstonchronicle.com", "chron.com",
    "expressnews.com", "statesman.com", "azcentral.com", "reviewjournal.com", "sltrib.com",
    "denverpost.com", "seattletimes.com", "oregonlive.com", "sfchronicle.com", "mercurynews.com",
    "sandiegouniontribune.com", "sacbee.com", "fresnobee.com", "ocregister.com", "startribune.com",
    "jsonline.com", "detroitnews.com", "freep.com", "cleveland.com", "dispatch.com",
    "cincinnati.com", "indystar.com", "stltoday.com", "kansascity.com", "omaha.com",
    "charlotteobserver.com", "newsobserver.com", "postandcourier.com", "richmond.com",
    "pilotonline.com", "buffalonews.com", "syracuse.com", "post-gazette.com", "triblive.com",
    "pennlive.com", "nj.com", "staradvertiser.com", "adn.com", "desmoinesregister.com",
    "oklahoman.com", "tulsaworld.com", "arkansasonline.com", "al.com", "clarionledger.com",
    "knoxnews.com", "commercialappeal.com", "ksl.com", "wral.com", "deseret.com",
)

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
    "mugshot_url",
    "mugshot_checked",
    "source_name",
    "source_url",
    "additional_sources",
    "confidence",
    "corroboration_checked",  # date the second-source search last ran for this row
]

OFFENDER_COLUMNS = [
    "offender_key",
    "offender_name",
    "state",
    "story_ids",
    "incident_count",
    "first_incident_date",
    "last_incident_date",
    "latest_prior_arrests",
    "latest_prior_convictions",
    "latest_prior_felony_convictions",
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
