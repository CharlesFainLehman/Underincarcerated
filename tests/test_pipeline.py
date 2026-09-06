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
        monkeypatch.setattr(mod, "BACKFILL_STORIES_CSV", data / "backfill.csv", raising=False)
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
    by_id = {r["id"]: r for r in json.loads((site / "stories.json").read_text())}
    assert by_id[1]["prior_count_arrests"] == 11
    assert by_id[1]["qualifies_strict"] is True
    assert by_id[2]["prior_count_convictions"] is None

    # Corrupt the strict flag and confirm validation catches it.
    rows = store.load_stories()
    rows[1]["qualifies_strict"] = "yes"
    store.save_stories(rows)
    with pytest.raises(SystemExit):
        validate_data.main()


def test_process_checkpoint_and_cap(monkeypatch, tmp_path):
    stories: list[dict] = []
    cands = [{"url": f"https://x.com/{i}", "title": "t", "source": "x.com"} for i in range(60)]
    monkeypatch.setattr(process, "CHECKPOINT_EVERY", 10)
    monkeypatch.setattr(process, "triage_candidates", lambda c, cs: [True] * len(cs))
    monkeypatch.setattr(process, "fetch_article_text", lambda url: ARTICLE)
    monkeypatch.setattr(process, "classify_article",
                        lambda c, cand, t: _cls(offender_name=f"Person {cand['url'][-2:]}"))
    monkeypatch.setattr(process, "check_duplicate",
                        lambda c, row, st: DedupeResult(relation="unrelated"))
    monkeypatch.setattr(process, "resolve_candidate", lambda cand: None)
    calls = []
    seen: set[str] = set()
    counts = process_candidates(FakeClient(), cands, stories, seen,
                                checkpoint=lambda: calls.append(len(stories)), max_classify=35)
    assert counts["new"] == 35
    assert calls == [10, 20, 30, 35]      # after every chunk, last one partial
    assert len(seen) == 35                # deferred candidates are not marked seen


def test_gdelt_split_on_cap(monkeypatch):
    """A capped window is re-queried on each half; an uncapped one is not."""
    import fetch
    from datetime import datetime
    calls = []

    def fake_search(query, start, end, max_records=250):
        calls.append((start, end))
        hours = (end - start).total_seconds() / 3600
        n = 250 if hours >= 24 else 40
        return [{"url": f"https://x.com/{start:%Y%m%d%H}/{i}", "title": "t"} for i in range(n)]

    monkeypatch.setattr(fetch, "gdelt_search", fake_search)
    monkeypatch.setattr(fetch.time, "sleep", lambda s: None)
    out = fetch.gdelt_search_split("q", datetime(2026, 9, 1), datetime(2026, 9, 4))
    assert len(calls) == 7            # 72h capped -> 2x36h capped -> 4x18h fine
    assert len(out) == 160
    assert len({c["url"] for c in out}) == 160


def test_triage_runs_before_resolution(monkeypatch, tmp_path):
    """Only triage-kept candidates pay for Google redirect decoding."""
    resolved = []
    stories: list[dict] = []
    cands = [{"url": "https://news.google.com/rss/articles/A", "title": "policy piece", "source": "s"},
             {"url": "https://news.google.com/rss/articles/B", "title": "man arrested", "source": "s"}]

    def fake_resolve(cand):
        resolved.append(cand["url"])
        cand["google_url"] = cand["url"]
        cand["url"] = "https://real.com/story"

    monkeypatch.setattr(process, "triage_candidates", lambda c, cs: [False, True])
    monkeypatch.setattr(process, "resolve_candidate", fake_resolve)
    monkeypatch.setattr(process, "fetch_article_text", lambda url: ARTICLE)
    monkeypatch.setattr(process, "classify_article", lambda c, cand, t: _cls())
    monkeypatch.setattr(process, "check_duplicate", lambda c, r, s: DedupeResult(relation="unrelated"))
    seen: set[str] = set()
    counts = process_candidates(FakeClient(), cands, stories, seen)
    assert resolved == ["https://news.google.com/rss/articles/B"]
    assert counts["triaged_out"] == 1 and counts["new"] == 1
    assert stories[0]["source_url"] == "https://real.com/story"
    assert {"https://news.google.com/rss/articles/A", "https://news.google.com/rss/articles/B",
            "https://real.com/story"} <= seen


def test_google_headline_duplicates_skip_resolution(monkeypatch):
    """A Google News item whose headline matches a direct-URL candidate is a
    duplicate of that article; it is dropped without decoding the redirect."""
    resolved = []
    stories: list[dict] = []
    cands = [{"url": "https://direct.com/a", "title": "Man with 12 prior arrests charged", "source": "d"},
             {"url": "https://news.google.com/rss/articles/X",
              "title": "Man with 12 prior arrests charged - Direct News", "source": "Direct News"},
             {"url": "https://news.google.com/rss/articles/Y", "title": "Different story", "source": "o"}]

    def fake_resolve(cand):
        resolved.append(cand["url"])
        cand["google_url"] = cand["url"]
        cand["url"] = "https://other.com/y"

    monkeypatch.setattr(process, "triage_candidates", lambda c, cs: [True] * len(cs))
    monkeypatch.setattr(process, "resolve_candidate", fake_resolve)
    monkeypatch.setattr(process, "fetch_article_text", lambda url: ARTICLE)
    monkeypatch.setattr(process, "classify_article",
                        lambda c, cand, t: _cls(offender_name="A B" if "direct" in cand["url"] else "C D"))
    monkeypatch.setattr(process, "check_duplicate", lambda c, r, s: DedupeResult(relation="unrelated"))
    seen: set[str] = set()
    counts = process_candidates(FakeClient(), cands, stories, seen)
    assert resolved == ["https://news.google.com/rss/articles/Y"]
    assert counts["duplicates"] == 1 and counts["new"] == 2
    assert "https://news.google.com/rss/articles/X" in seen


def test_parse_classification_tolerates_fences_and_coerces():
    from classify import ClassificationParseError, parse_classification
    reply = """```json
{"qualifies": true, "reason": "r", "incident_date": "2026-09-01", "city": "X", "state": "oh",
 "offender_name": "A B", "age": "34", "new_offense_type": "sexual battery",
 "new_offense_severity": "violent", "prior_count_arrests": "11", "prior_count_convictions": null,
 "prior_count_felony_convictions": 3, "prior_offenses": "burglary", "prior_evidence_quote": "q",
 "release_status": "bond", "release_evidence_quote": null, "releasing_jurisdiction": null,
 "outcome": "charged", "summary": "s", "confidence": "very high"}
```"""
    c = parse_classification(reply)
    assert c.state == "OH" and c.age == 34 and c.prior_count_arrests == 11
    assert c.new_offense_type == "other"          # unknown enum -> catch-all
    assert c.release_status == "none stated"      # unknown enum -> none stated
    assert c.confidence == "low"
    with pytest.raises(ClassificationParseError):
        parse_classification("Sorry, I cannot help with that.")
    with pytest.raises(ClassificationParseError):
        parse_classification('{"qualifies": "maybe"}')


def test_classify_article_uses_plain_text_reply(monkeypatch):
    """classify_article must not use structured-output mode (schema too complex)."""
    import classify
    from types import SimpleNamespace
    captured = {}

    class FakeMessages:
        def create(self, **kw):
            captured.update(kw)
            return SimpleNamespace(content=[SimpleNamespace(
                type="text", text='{"qualifies": false, "reason": "policy piece"}')])

        def parse(self, **kw):
            raise AssertionError("structured outputs must not be used for classification")

    client = SimpleNamespace(messages=FakeMessages())
    c = classify.classify_article(client, {"title": "t", "source": "s", "published": "p"}, "text")
    assert c.qualifies is False and "output_format" not in captured
    assert "JSON schema" in captured["system"]


def test_pack_decisions(tmp_path, monkeypatch):
    import gzip
    import pack_decisions
    monkeypatch.setattr(pack_decisions, "DECISIONS_DIR", tmp_path)
    (tmp_path / "2026-09-05.jsonl").write_text('{"a":1}\n')
    old = tmp_path / "2026-01-01.jsonl.gz"
    old.write_bytes(b"x")
    import os, time
    os.utime(old, (time.time() - 40 * 86400,) * 2)
    pack_decisions.main()
    assert not (tmp_path / "2026-09-05.jsonl").exists()
    assert gzip.open(tmp_path / "2026-09-05.jsonl.gz").read() == b'{"a":1}\n'
    assert not old.exists()


def test_obvious_same_incident_and_stateless_match():
    from dedupe import check_duplicate, obvious_same_incident, same_person
    a = {"offender_key": "simpson_david_NC", "state": "NC", "incident_date": "2026-07-29", "id": "45"}
    b = {"offender_key": "simpson_david_", "state": "", "incident_date": "2026-08-03"}
    c = {"offender_key": "simpson_david_TX", "state": "TX", "incident_date": "2026-07-29"}
    d = {"offender_key": "simpson_david_NC", "state": "NC", "incident_date": "2026-01-01"}
    assert same_person(a, b) and not same_person(a, c)
    assert obvious_same_incident(b, a)
    assert not obvious_same_incident(d, a)        # same person, 7 months apart
    # Deterministic merge never calls the model.
    class NoCalls:
        class messages:
            @staticmethod
            def parse(**kw):
                raise AssertionError("model should not be called")
    r = check_duplicate(NoCalls(), b, [a])
    assert r.relation == "same_incident" and r.matching_id == "45"


def test_classify_splits_multi_name(monkeypatch):
    import classify
    from types import SimpleNamespace
    reply = ('{"qualifies": true, "reason": "r", "state": "AL", "offender_name": "Tarus Giles Jr.; William Wingard",'
             ' "prior_evidence_quote": "q"}')
    client = SimpleNamespace(messages=SimpleNamespace(
        create=lambda **kw: SimpleNamespace(content=[SimpleNamespace(type="text", text=reply)])))
    c = classify.classify_article(client, {"title": "t", "source": "s", "published": "p"}, "text")
    assert c.offender_name == "Tarus Giles Jr."


def test_fetch_skips_video_pages(monkeypatch):
    import fetch
    monkeypatch.setattr(fetch.trafilatura, "fetch_url", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no fetch")))
    assert fetch.fetch_article_text("https://www.foxnews.com/video/6402730125112") is None


def test_exports_merge_daily_and_backfill(monkeypatch, tmp_path):
    data = tmp_path / "data"
    (data / "backfill").mkdir(parents=True)
    site = tmp_path / "site"
    for mod in (store, validate_data):
        monkeypatch.setattr(mod, "STORIES_CSV", data / "stories.csv")
        monkeypatch.setattr(mod, "BACKFILL_STORIES_CSV", data / "backfill" / "stories.csv")
    monkeypatch.setattr(build_exports, "OFFENDERS_CSV", data / "offenders.csv")
    monkeypatch.setattr(build_exports, "SITE_DIR", site)
    monkeypatch.setattr(validate_data, "DATA_DIR", data)
    daily = make_row(1, _cls(), {"url": "https://x.com/a", "source": "x"})
    bf = make_row(1_000_001, _cls(offender_name="John Smith", incident_date="2019-05-01"),
                  {"url": "https://x.com/old", "source": "x"})
    store.save_stories([daily])
    store.save_stories([bf], data / "backfill" / "stories.csv")
    validate_data.main()
    build_exports.build_exports()
    rows = list(csv.DictReader(open(site / "stories.csv", newline="", encoding="utf-8")))
    assert [r["id"] for r in rows] == ["1000001", "1"]   # ordered by incident date
    offenders = list(csv.DictReader(open(data / "offenders.csv", newline="", encoding="utf-8")))
    assert offenders[0]["incident_count"] == "2"            # same person across both files
    # A backfill id in the daily file fails validation.
    store.save_stories([daily, dict(daily, id="1000002", source_url="https://x.com/b")])
    with pytest.raises(SystemExit):
        validate_data.main()


def test_backfill_skips_done_weeks(monkeypatch, tmp_path):
    import backfill
    monkeypatch.setattr(backfill, "BACKFILL_DONE_WEEKS_JSON", tmp_path / "done.json")
    backfill.save_done_weeks({"2017-01-01"})
    assert backfill.load_done_weeks() == {"2017-01-01"}


def test_quarantine_moves_bad_rows(monkeypatch, tmp_path):
    import quarantine
    data = tmp_path / "data"
    data.mkdir()
    for mod in (store, validate_data, quarantine):
        monkeypatch.setattr(mod, "STORIES_CSV", data / "stories.csv")
        monkeypatch.setattr(mod, "BACKFILL_STORIES_CSV", data / "backfill.csv")
    monkeypatch.setattr(quarantine, "QUARANTINE_CSV", data / "quarantine.csv")
    monkeypatch.setattr(validate_data, "DATA_DIR", data)
    good = make_row(1, _cls(), {"url": "https://x.com/a", "source": "x"})
    bad = make_row(2, _cls(incident_date="2099-01-01"), {"url": "https://x.com/b", "source": "x"})
    store.save_stories([good, bad])
    with pytest.raises(SystemExit):
        validate_data.main()
    quarantine.main()
    validate_data.main()
    assert [r["id"] for r in store.load_stories()] == ["1"]
    q = list(csv.DictReader(open(data / "quarantine.csv", newline="", encoding="utf-8")))
    assert q[0]["id"] == "2" and "future" in q[0]["reason"]


def test_future_incident_date_blanked_at_ingest(monkeypatch, tmp_path):
    stories: list[dict] = []
    cands = [{"url": "https://x.com/a", "title": "t", "source": "x.com"}]
    counts, _ = _run(monkeypatch, tmp_path, cands, stories, _cls(incident_date="2099-01-01"))
    assert counts["new"] == 1 and stories[0]["incident_date"] == ""


def test_headline_priority_orders_counts_first():
    from process import headline_priority
    titles = ["Local man arrested after bar fight",
              "Repeat offender charged in Apopka",
              "Man with 12 prior arrests charged in shooting",
              "Suspect out on bond arrested again"]
    ranked = sorted(titles, key=lambda t: headline_priority({"title": t}), reverse=True)
    assert ranked[0].startswith("Man with 12") and ranked[-1].startswith("Local man")


def test_google_news_search_adds_date_operators(monkeypatch):
    import fetch
    from datetime import datetime
    seen_urls = []
    monkeypatch.setattr(fetch.feedparser, "parse",
                        lambda url: (seen_urls.append(url), type("F", (), {"entries": []})())[1])
    fetch.google_news_search('"repeat offender" arrested', datetime(2025, 1, 1), datetime(2025, 1, 7))
    assert "after%3A2025-01-01" in seen_urls[0] and "before%3A2025-01-07" in seen_urls[0]
    fetch.google_news_search('"repeat offender" arrested')
    assert "after" not in seen_urls[1]


def test_process_cap_ranks_before_resolution(monkeypatch, tmp_path):
    """With a cap, only the best headlines (times a margin) are decoded."""
    resolved = []
    stories: list[dict] = []
    cands = [{"url": f"https://news.google.com/rss/articles/{i}", "title": "plain headline", "source": "s"}
             for i in range(40)]
    cands.append({"url": "https://news.google.com/rss/articles/best",
                  "title": "Man with 12 prior arrests charged", "source": "s"})

    def fake_resolve(cand):
        resolved.append(cand["url"])
        cand["google_url"] = cand["url"]
        cand["url"] = "https://real.com/" + cand["google_url"].rsplit("/", 1)[1]

    monkeypatch.setattr(process, "triage_candidates", lambda c, cs: [True] * len(cs))
    monkeypatch.setattr(process, "resolve_candidate", fake_resolve)
    monkeypatch.setattr(process, "fetch_article_text", lambda url: ARTICLE)
    monkeypatch.setattr(process, "classify_article", lambda c, cand, t: _cls(offender_name="P " + cand["url"][-3:]))
    monkeypatch.setattr(process, "check_duplicate", lambda c, r, s: DedupeResult(relation="unrelated"))
    counts = process_candidates(FakeClient(), cands, stories, set(), max_classify=10)
    assert resolved[0].endswith("/best")
    assert len(resolved) == 10 * 1.3 + 5 and counts["new"] == 10


def test_undecoded_google_urls_are_not_marked_seen(monkeypatch, tmp_path):
    """A redirect Google refuses to decode is skipped and left unseen; a
    batch that is mostly refused raises after the retry wait."""
    from process import ResolutionThrottled
    monkeypatch.setattr(process.time, "sleep", lambda s: None)
    monkeypatch.setattr(process, "triage_candidates", lambda c, cs: [True] * len(cs))
    monkeypatch.setattr(process, "resolve_candidate", lambda cand: None)   # decoder refuses
    monkeypatch.setattr(process, "fetch_article_text", lambda url: ARTICLE)
    monkeypatch.setattr(process, "classify_article", lambda c, cand, t: _cls())
    monkeypatch.setattr(process, "check_duplicate", lambda c, r, s: DedupeResult(relation="unrelated"))
    stories: list[dict] = []
    seen: set[str] = set()
    cands = [{"url": f"https://news.google.com/rss/articles/{i}", "title": f"headline {i}", "source": "s"}
             for i in range(10)]
    with pytest.raises(ResolutionThrottled):
        process_candidates(FakeClient(), cands, stories, seen)
    assert not seen and not stories
    # A single refusal among decodable ones is skipped, not seen, not fatal.
    def resolve_most(cand):
        if not cand["url"].endswith("/0"):
            cand["google_url"] = cand["url"]
            cand["url"] = "https://real.com/" + cand["url"].rsplit("/", 1)[1]
    monkeypatch.setattr(process, "resolve_candidate", resolve_most)
    counts = process_candidates(FakeClient(), cands, stories, seen)
    assert counts["unresolved"] == 1 and counts["new"] == 9
    assert "https://news.google.com/rss/articles/0" not in seen


def test_candidate_images_ranks_mugshot_first():
    from mugshots import candidate_images
    html = '''<html><head><meta property="og:image" content="https://cdn.x.com/og.jpg"></head><body>
    <img src="/static/logo.png" alt="Station logo">
    <img src="/img/booking-smith.jpg" alt="Booking photo of John Smith" width="600">
    <img src="/img/scene.jpg" alt="Police at the scene">
    <img src="/img/tiny.jpg" width="40">
    </body></html>'''
    out = candidate_images(html, "https://x.com/story", "Smith")
    assert out[0] == "https://x.com/img/booking-smith.jpg"
    assert "https://cdn.x.com/og.jpg" in out
    assert not any("logo" in u or "tiny" in u for u in out)


def test_sweep_stores_url_and_marks_checked(monkeypatch, tmp_path):
    import mugshots
    from types import SimpleNamespace
    path = tmp_path / "s.csv"
    rows = [make_row(1, _cls(), {"url": "https://x.com/a", "source": "x"}),
            make_row(2, _cls(offender_name="Jane Doe"), {"url": "https://x.com/b", "source": "x"})]
    store.save_stories(rows, path)
    monkeypatch.setattr(mugshots, "fetch_html",
                        lambda url: '<img src="/m.jpg" alt="mugshot">' if url.endswith("/a") else None)
    monkeypatch.setattr(mugshots, "is_mugshot", lambda c, u: u.endswith("/m.jpg"))
    counts = mugshots.sweep(SimpleNamespace(), path)
    got = {r["id"]: r for r in store.load_stories(path)}
    assert counts == {"checked": 2, "found": 1}
    assert got["1"]["mugshot_url"] == "https://x.com/m.jpg" and got["1"]["mugshot_checked"]
    assert got["2"]["mugshot_url"] == "" and got["2"]["mugshot_checked"]
    # Second sweep skips checked rows.
    assert mugshots.sweep(SimpleNamespace(), path) == {"checked": 0, "found": 0}
