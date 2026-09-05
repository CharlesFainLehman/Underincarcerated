"""Offline tests for the parts of the pipeline that need no network or model."""

import csv
import json
from types import SimpleNamespace

import pytest

import build_exports
import config
import process
import store
import validate_data
from classify import StoryClassification, check_evidence, qualifies_strict, quote_in_text
from dedupe import DedupeResult, find_candidates
from process import canonical_url, process_candidates, syndication_path
from store import make_row, offender_key


ARTICLE = (
    "SPRINGFIELD, Ohio (WXYZ) - A Springfield man was arrested Tuesday after police say "
    "he robbed a gas station on Main Street. John M. Smith Jr., 34, was charged with "
    "aggravated robbery. Court records show Smith has been arrested 11 times since 2015, "
    "including three prior felony convictions for burglary and assault. He was out on bond "
    "in a separate Clark County drug case at the time of the robbery, prosecutors said. "
    "Smith is being held at the Clark County Jail."
)


def test_canonical_url_strips_tracking_and_variants():
    a = "https://example.com/news/story/?utm_source=x&id=5#top"
    b = "https://example.com/amp/news/story?id=5"
    assert canonical_url(a) == canonical_url(b) == "https://example.com/news/story?id=5"


def test_syndication_path():
    assert syndication_path("https://a.com/2026/09/04/man-arrested/") == "/2026/09/04/man-arrested"
    assert syndication_path("https://a.com/story/12345") is None


def test_offender_key_normalizes():
    assert offender_key("John M. Smith Jr.", "oh") == "smith_john_OH"
    assert offender_key("John Smith", "OH") == "smith_john_OH"
    assert offender_key("José Álvarez-García", "TX") == "garcia_jose_TX"
    assert offender_key("Cher", "CA") == ""
    assert offender_key(None, "CA") == ""


def test_quote_in_text_tolerates_quotes_and_whitespace():
    q = "Court records show Smith has been arrested 11 times since 2015, including three prior felony convictions"
    assert quote_in_text(q, ARTICLE)
    assert quote_in_text(q.replace("Smith has", "Smith  has"), ARTICLE)
    assert quote_in_text("“He was out on bond in a separate Clark County drug case”", ARTICLE)


def test_quote_in_text_rejects_paraphrase_and_short():
    assert not quote_in_text("Smith has eleven prior arrests and three felonies", ARTICLE)
    assert not quote_in_text("arrested 11 times", ARTICLE)  # too short to be evidence
    assert not quote_in_text(None, ARTICLE)


def _cls(**over) -> StoryClassification:
    base = dict(
        qualifies=True, reason="r", incident_date="2026-09-01", city="Springfield", state="OH",
        offender_name="John M. Smith Jr.", age=34, new_offense_type="robbery",
        new_offense_severity="violent", prior_count_arrests=11, prior_count_convictions=None,
        prior_count_felony_convictions=3, prior_offenses="burglary; assault",
        prior_evidence_quote="Court records show Smith has been arrested 11 times since 2015, "
                             "including three prior felony convictions for burglary and assault.",
        release_status="bail/bond",
        release_evidence_quote="He was out on bond in a separate Clark County drug case at the "
                               "time of the robbery, prosecutors said.",
        releasing_jurisdiction="Clark County", outcome="charged",
        summary="Smith robbed a gas station while out on bond.", confidence="high",
    )
    base.update(over)
    return StoryClassification(**base)


def test_check_evidence_passes_verbatim():
    assert check_evidence(_cls(), ARTICLE) is None


def test_check_evidence_rejects_invented_quote():
    bad = _cls(prior_evidence_quote="Smith has a long rap sheet with 11 arrests and 3 felonies.")
    assert "prior_evidence_quote" in check_evidence(bad, ARTICLE)


def test_check_evidence_requires_release_quote():
    assert "without a quote" in check_evidence(_cls(release_evidence_quote=None), ARTICLE)
    assert check_evidence(_cls(release_status="none stated", release_evidence_quote=None), ARTICLE) is None
    assert "no prior record" in check_evidence(
        _cls(prior_evidence_quote=None, release_status="none stated", release_evidence_quote=None), ARTICLE)


def test_qualifies_strict_thresholds():
    assert qualifies_strict(5, None, None)
    assert qualifies_strict(None, 5, None)
    assert qualifies_strict(None, None, 3)
    assert not qualifies_strict(4, 4, 2)
    assert not qualifies_strict(None, None, None)


def test_make_row_fields():
    row = make_row(7, _cls(), {"url": "https://x.com/a", "source": "x.com"})
    assert row["id"] == "7"
    assert row["offender_key"] == "smith_john_OH"
    assert row["qualifies_strict"] == "yes"
    assert row["prior_count_convictions"] == ""
    assert row["age"] == "34"
    assert set(row) == set(config.CSV_COLUMNS)


def test_find_candidates_ranks_same_person_first():
    stories = [
        {"id": "1", "state": "OH", "incident_date": "2026-09-01", "offender_key": "doe_jane_OH", "date_added": "2026-09-02"},
        {"id": "2", "state": "OH", "incident_date": "2025-01-01", "offender_key": "smith_john_OH", "date_added": "2025-01-02"},
        {"id": "3", "state": "TX", "incident_date": "2026-09-01", "offender_key": "", "date_added": "2026-09-02"},
    ]
    new = {"state": "OH", "incident_date": "2026-09-03", "offender_key": "smith_john_OH"}
    ids = [s["id"] for s in find_candidates(new, stories)]
    assert ids == ["2", "1"]  # same person (old date) outranks nearby date; other state excluded


class FakeClient:
    """Stands in for anthropic.Anthropic; process_candidates only passes it through."""


def _run(monkeypatch, tmp_path, candidates, stories, classification, dedupe=None, triage=None,
         text=ARTICLE):
    monkeypatch.setattr(process, "triage_candidates", lambda c, cands: triage or [True] * len(cands))
    monkeypatch.setattr(process, "fetch_article_text", lambda url: text)
    monkeypatch.setattr(process, "classify_article", lambda c, cand, t: classification)
    monkeypatch.setattr(process, "check_duplicate",
                        lambda c, row, st: dedupe or DedupeResult(relation="unrelated"))
    monkeypatch.setattr(process, "resolve_candidate", lambda cand: None)
    seen: set[str] = set()
    counts = process_candidates(FakeClient(), candidates, stories, seen,
                                decision_log=tmp_path / "log.jsonl")
    return counts, seen


def test_process_adds_verified_row(monkeypatch, tmp_path):
    stories: list[dict] = []
    cands = [{"url": "https://x.com/a", "title": "Man arrested", "source": "x.com"}]
    counts, seen = _run(monkeypatch, tmp_path, cands, stories, _cls())
    assert counts["new"] == 1 and len(stories) == 1
    assert stories[0]["qualifies_strict"] == "yes"
    assert "https://x.com/a" in seen
    log = [json.loads(l) for l in (tmp_path / "log.jsonl").read_text().splitlines()]
    assert [r["stage"] for r in log] == ["triage", "classify"]


def test_process_rejects_unverifiable_quote(monkeypatch, tmp_path):
    stories: list[dict] = []
    cands = [{"url": "https://x.com/a", "title": "t", "source": "x.com"}]
    bad = _cls(prior_evidence_quote="A sentence that is not in the article at all, really.")
    counts, seen = _run(monkeypatch, tmp_path, cands, stories, bad)
    assert counts["rejected"] == 1 and not stories
    assert "https://x.com/a" in seen  # verified-bad is final, not retried


def test_process_rejects_non_us_state(monkeypatch, tmp_path):
    stories: list[dict] = []
    cands = [{"url": "https://x.com/a", "title": "t", "source": "x.com"}]
    counts, _ = _run(monkeypatch, tmp_path, cands, stories, _cls(state="ON"))
    assert counts["rejected"] == 1 and not stories


def test_process_no_text_marks_seen_without_row(monkeypatch, tmp_path):
    stories: list[dict] = []
    cands = [{"url": "https://x.com/a", "title": "t", "source": "x.com"}]
    counts, seen = _run(monkeypatch, tmp_path, cands, stories, _cls(), text=None)
    assert counts["no_text"] == 1 and not stories and "https://x.com/a" in seen


def test_process_triage_drops_without_fetch(monkeypatch, tmp_path):
    stories: list[dict] = []
    cands = [{"url": "https://x.com/a", "title": "t", "source": "x.com"}]
    counts, seen = _run(monkeypatch, tmp_path, cands, stories, _cls(), triage=[False])
    assert counts["triaged_out"] == 1 and not stories and "https://x.com/a" in seen


def test_process_same_incident_merges_sources_and_counts(monkeypatch, tmp_path):
    first = make_row(1, _cls(prior_count_arrests=2, prior_count_felony_convictions=None),
                     {"url": "https://x.com/a", "source": "x.com"})
    stories = [first]
    cands = [{"url": "https://y.com/b", "title": "t", "source": "y.com"}]
    counts, _ = _run(monkeypatch, tmp_path, cands, stories, _cls(),
                     dedupe=DedupeResult(relation="same_incident", matching_id="1"))
    assert counts["duplicates"] == 1 and len(stories) == 1
    assert stories[0]["additional_sources"] == "https://y.com/b"
    assert stories[0]["prior_count_arrests"] == "11"
    assert stories[0]["qualifies_strict"] == "yes"


def test_process_same_person_shares_key(monkeypatch, tmp_path):
    first = make_row(1, _cls(offender_name="Johnny Smith"), {"url": "https://x.com/a", "source": "x.com"})
    first["offender_key"] = "smith_johnny_OH"
    stories = [first]
    cands = [{"url": "https://y.com/b", "title": "t", "source": "y.com"}]
    counts, _ = _run(monkeypatch, tmp_path, cands, stories, _cls(),
                     dedupe=DedupeResult(relation="same_person_new_incident", matching_id="1"))
    assert counts["new"] == 1 and counts["same_person"] == 1
    assert stories[1]["offender_key"] == "smith_johnny_OH"


def test_process_skips_stored_url_variants(monkeypatch, tmp_path):
    first = make_row(1, _cls(), {"url": "https://x.com/2026/09/01/story", "source": "x.com"})
    stories = [first]
    cands = [{"url": "https://x.com/2026/09/01/story?utm_source=tw", "title": "t", "source": "x.com"},
             {"url": "https://sibling.com/2026/09/01/story", "title": "t", "source": "sibling.com"}]
    counts, _ = _run(monkeypatch, tmp_path, cands, stories, _cls())
    assert counts["duplicates"] == 2 and len(stories) == 1


def test_exports_and_validation(monkeypatch, tmp_path):
    data = tmp_path / "data"
    site = tmp_path / "site"
    data.mkdir()
    for mod in (store, build_exports, validate_data, config):
        monkeypatch.setattr(mod, "STORIES_CSV", data / "stories.csv", raising=False)
    monkeypatch.setattr(build_exports, "OFFENDERS_CSV", data / "offenders.csv")
    monkeypatch.setattr(build_exports, "SITE_DIR", site)
    monkeypatch.setattr(validate_data, "DATA_DIR", data)

    r1 = make_row(1, _cls(), {"url": "https://x.com/a", "source": "x.com"})
    r2 = make_row(2, _cls(incident_date="2026-08-20", prior_count_arrests=1,
                          prior_count_felony_convictions=None),
                  {"url": "https://x.com/b", "source": "x.com"})
    r3 = make_row(3, _cls(offender_name="Jane Doe", state="TX", prior_count_arrests=2,
                          prior_count_felony_convictions=None),
                  {"url": "https://x.com/c", "source": "x.com"})
    store.save_stories([r1, r2, r3])
    validate_data.main()

    build_exports.build_exports()
    offenders = list(csv.DictReader(open(data / "offenders.csv", newline="", encoding="utf-8")))
    assert offenders[0]["offender_key"] == "smith_john_OH"
    assert offenders[0]["incident_count"] == "2"
    assert offenders[0]["story_ids"] == "2 1"  # ordered by incident_date
    assert offenders[0]["max_prior_arrests"] == "11"
    assert offenders[0]["qualifies_strict"] == "yes"
    assert offenders[1]["qualifies_strict"] == "no"
    stats = json.loads((site / "stats.json").read_text())
    assert stats["stories"] == 3 and stats["stories_strict"] == 1
    assert stats["offenders_multiple_incidents"] == 1
    stories_json = json.loads((site / "stories.json").read_text())
    assert stories_json[0]["prior_count_arrests"] == 11
    assert stories_json[0]["qualifies_strict"] is True
    assert stories_json[1]["prior_count_convictions"] is None

    # Corrupt the strict flag and confirm validation catches it.
    rows = store.load_stories()
    rows[1]["qualifies_strict"] = "yes"
    store.save_stories(rows)
    with pytest.raises(SystemExit):
        validate_data.main()
