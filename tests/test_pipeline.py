"""Tests for timing, the LinkedIn client, the store and a full run, with LinkedIn faked."""

import threading

import pytest
from fastapi.testclient import TestClient

from illuminate import timing
from illuminate.classifier import classify
from illuminate.config import Config
from illuminate.linkedin import Card, LinkedInClient, RateLimited, parse_cards
from illuminate.service import Service
from illuminate.store import Store
from illuminate.web import create_app

EMPTY = "<!DOCTYPE html>\n\n<!---->"


def card_html(job_id, title, company="Acme", location="Pune, India", label="5 minutes ago"):
    return f"""
    <li><div class="base-card base-search-card job-search-card" data-entity-urn="urn:li:jobPosting:{job_id}">
      <a class="base-card__full-link" href="https://in.linkedin.com/jobs/view/x-{job_id}?refId=1"></a>
      <div class="base-search-card__info">
        <h3 class="base-search-card__title"> {title} </h3>
        <h4 class="base-search-card__subtitle">
          <a href="https://in.linkedin.com/company/acme?trk=x"> {company} </a></h4>
        <div class="base-search-card__metadata">
          <span class="job-search-card__location"> {location} </span>
          <time class="job-search-card__listdate--new" datetime="2026-11-15"> {label} </time>
        </div></div></div></li>"""


class FakeLinkedIn:
    """Serves jobs as LinkedIn would: filtered by time window, ten per page."""

    def __init__(self, clock):
        self.clock = clock
        self.jobs = []  # (id, title, company, posted_at)
        self.calls = []
        self.fail_with = None
        self.hidden_from = {}  # keywords -> job ids LinkedIn's matching skips

    def add(self, job_id, title, age_seconds, company="Acme"):
        self.jobs.append((job_id, title, company, self.clock() - age_seconds))

    def __call__(self, params):
        self.calls.append(params)
        if self.fail_with:
            return self.fail_with, ""
        window = int(params["f_TPR"][1:])
        now = self.clock()
        keywords = params.get("keywords", "").lower()
        hits = [
            job for job in sorted(self.jobs, key=lambda j: -j[3])
            if now - job[3] <= window
            and job[0] not in self.hidden_from.get(keywords, ())
            and (keywords == "product manager"  # stands for a broad search
                 or all(word in f"{job[1]} {job[2]}".lower() for word in keywords.split()))
        ]
        page = hits[params["start"]:params["start"] + 10]
        if not page:
            return 200, EMPTY

        def label(age):
            minutes = int(age // 60)
            if minutes < 55:
                return f"{minutes} minutes ago"
            return f"{max(1, round(age / 3600))} hours ago"

        return 200, "<!DOCTYPE html>" + "".join(
            card_html(j[0], j[1], j[2], label=label(now - j[3])) for j in page
        )


class Clock:
    def __init__(self, start=1_800_000_000.0):
        self.now = start

    def __call__(self):
        self.now += 1  # every look at the clock costs a second
        return self.now


@pytest.fixture
def world(monkeypatch):
    clock = Clock()
    monkeypatch.setattr("time.time", clock)
    monkeypatch.setattr("random.randint", lambda low, high: 1)  # windows: one second longer
    fake = FakeLinkedIn(clock)
    client = LinkedInClient(min_delay=0, max_delay=0, retry_waits=(0, 0), fetch=fake, clock=clock)
    store = Store(":memory:")
    config = Config(queries=["product manager"], full_scan=False, refine_requests_per_cycle=40)
    return clock, fake, store, Service(config, store, client)


# ---- timing ---------------------------------------------------------------


def test_minutes_label_is_precise():
    lo, hi = timing.bounds_from_label("49 minutes ago", 10_000)
    assert lo <= 10_000 - 49 * 60 - 30 <= hi
    assert timing.is_precise((lo, hi))


def test_hours_label_is_not_precise():
    bounds = timing.bounds_from_label("3 hours ago", 100_000)
    assert bounds[0] <= 100_000 - 3.5 * 3600 <= bounds[1]
    assert not timing.is_precise(bounds)


def test_two_sightings_tighten_the_estimate():
    posted = 50_000.0
    first = timing.bounds_from_label("12 minutes ago", posted + 12 * 60 + 50)
    second = timing.bounds_from_label("27 minutes ago", posted + 27 * 60 + 5)
    merged = timing.merge(first, second)
    assert merged[0] <= posted <= merged[1]
    assert merged[1] - merged[0] < first[1] - first[0]


def test_repost_replaces_old_bounds():
    assert timing.merge((100, 200), (5000, 5100)) == (5000, 5100)
    assert timing.merge((5000, 5100), (100, 200)) == (5000, 5100)


def test_unknown_label_gives_no_bounds():
    assert timing.bounds_from_label("Actively hiring", 1000) is None
    assert timing.bounds_from_label(None, 1000) is None


# ---- LinkedIn client ------------------------------------------------------


def test_windows_are_never_a_whole_number_of_minutes():
    from illuminate.linkedin import uncached_window

    for seconds in (900, 1800, 3600, 86400, 1741, 59):
        for _ in range(200):
            window = uncached_window(seconds)
            assert window % 60 != 0 and seconds < window <= seconds + 60



def test_parse_cards():
    cards = parse_cards(card_html("42", "Senior Product Manager", "Initech", "Remote, India", "2 hours ago"), 123.0)
    assert cards == [
        Card("42", "Senior Product Manager", "Initech", "https://in.linkedin.com/company/acme",
             "Remote, India", "2 hours ago", 123.0)
    ]
    assert cards[0].url == "https://www.linkedin.com/jobs/view/42"


def test_search_follows_every_page(world):
    clock, fake, store, service = world
    for i in range(23):
        fake.add(str(i), "Product Manager", age_seconds=60 * i)
    result = service.client.search("product manager", "India", 3600)
    assert len(result.cards) == 23 and result.complete
    assert [call["start"] for call in fake.calls] == [0, 10, 20, 23]


def test_rate_limit_is_reported_not_raised(world):
    clock, fake, store, service = world
    fake.fail_with = 429
    result = service.client.search("product manager", "India", 3600)
    assert not result.complete and result.rate_limited
    assert len(fake.calls) == 3  # first try plus two retries


def test_rate_limit_error_type(world):
    clock, fake, store, service = world
    fake.fail_with = 429
    with pytest.raises(RateLimited):
        service.client.search_page("product manager", "India", 3600)


# ---- a full run -----------------------------------------------------------


def test_run_stores_labels_and_sorts_newest_first(world):
    clock, fake, store, service = world
    fake.add("1", "Product Manager", age_seconds=40 * 60)
    fake.add("2", "Head of Product", age_seconds=5 * 60)
    fake.add("3", "Project Manager", age_seconds=1 * 60)
    fake.add("4", "APM - Growth", age_seconds=20 * 60)
    summary = service.run_cycle()
    assert summary["status"] == "ok" and summary["new_jobs"] == 4 and summary["new_relevant"] == 3
    assert [job["id"] for job in store.jobs(["match", "maybe"])] == ["2", "4", "1"]
    assert store.counts() == {"match": 2, "maybe": 1, "no": 1}


def test_second_run_adds_only_new_jobs_and_uses_a_short_window(world):
    clock, fake, store, service = world
    fake.add("1", "Product Manager", age_seconds=600)
    service.run_cycle()
    assert fake.calls[0]["f_TPR"] == "r86401"  # first run: 24 hours back (never a round number)
    clock.now += 15 * 60
    fake.calls.clear()
    fake.add("2", "Group Product Manager", age_seconds=120)
    summary = service.run_cycle()
    assert summary["new_jobs"] == 1
    window = int(fake.calls[0]["f_TPR"][1:])
    assert 30 * 60 <= window <= 32 * 60  # the gap since the last run plus the overlap
    assert [job["id"] for job in store.jobs(["match"])] == ["2", "1"]


def test_incomplete_run_makes_the_next_one_reach_further_back(world):
    clock, fake, store, service = world
    service.run_cycle()
    clock.now += 15 * 60
    fake.fail_with = 429
    assert service.run_cycle()["status"] == "partial"
    fake.fail_with = None
    clock.now += 15 * 60
    fake.calls.clear()
    fake.add("9", "Product Manager", age_seconds=20 * 60)  # posted during the failed run
    service.run_cycle()
    assert int(fake.calls[0]["f_TPR"][1:]) >= 45 * 60
    assert [job["id"] for job in store.jobs(["match"])] == ["9"]


def test_old_job_is_pinned_down_to_the_minute(world):
    clock, fake, store, service = world
    age = 5 * 3600 + 17 * 60
    fake.add("7", "Principal Product Manager", age_seconds=age, company="Globex")
    posted = fake.jobs[0][3]
    service.run_cycle()
    job = store.jobs(["match"])[0]
    assert job["posted_lo"] <= posted <= job["posted_hi"]
    assert timing.is_precise((job["posted_lo"], job["posted_hi"]))
    assert abs(job["posted_at"] - posted) <= 60


def test_job_that_cannot_be_looked_up_is_left_approximate(world):
    clock, fake, store, service = world
    fake.add("8", "Product Manager", age_seconds=4 * 3600, company="Initech")
    service.config.refine_requests_per_cycle = 0
    service.run_cycle()
    fake.jobs.clear()  # the posting has since been taken down
    assert service.refine(10) == 2  # tried by title and company, then by company
    job = store.jobs(["match"])[0]
    assert job["refine_failed"] == 1 and not timing.is_precise((job["posted_lo"], job["posted_hi"]))


def test_full_scan_catches_what_keyword_search_skips(world):
    clock, fake, store, service = world
    service.config.full_scan = True
    fake.add("1", "Product Manager", age_seconds=10 * 60, company="Yes Bank")
    fake.add("2", "Video Editor", age_seconds=5 * 60)
    fake.add("3", "Sales Manager", age_seconds=5 * 60)
    fake.hidden_from["product manager"] = {"1", "2"}  # the keyword search leaves these out
    summary = service.run_cycle()
    assert [call.get("keywords") for call in fake.calls[:4]] == ["product manager", "product manager", None, None]
    assert [job["id"] for job in store.jobs(["match"])] == ["1"]
    assert store.jobs(["match"])[0]["found_by"] == "full scan"
    # The scan read all three titles but kept only the product role; the
    # keyword search's near-miss is kept for review.
    assert summary["scanned"] == 3
    assert [job["id"] for job in store.jobs(["no"])] == ["3"]


def test_full_scan_only_looks_back_an_hour(world):
    clock, fake, store, service = world
    service.config.full_scan = True
    service.run_cycle()
    windows = {call.get("keywords"): call["f_TPR"] for call in fake.calls}
    assert windows == {"product manager": "r86401", None: "r3601"}


def test_refine_falls_back_to_searching_by_company(world):
    clock, fake, store, service = world
    fake.add("5", "Product Manager", age_seconds=3 * 3600, company="Globex")
    fake.hidden_from["product manager globex"] = {"5"}
    posted = fake.jobs[0][3]
    service.run_cycle()
    job = store.jobs(["match"])[0]
    assert job["refine_failed"] == 0
    assert timing.is_precise((job["posted_lo"], job["posted_hi"])) and abs(job["posted_at"] - posted) <= 60


# ---- persistence and the web API -----------------------------------------


def test_jobs_survive_a_restart(tmp_path):
    path = tmp_path / "jobs.db"
    store = Store(path)
    card = Card("1", "Product Manager", "Acme", None, "Pune", "3 minutes ago", 1000.0)
    assert store.upsert(card, classify(card.title), (800.0, 860.0), "product manager") is True
    assert store.upsert(card, classify(card.title), (800.0, 860.0), "product manager") is False
    store.close()
    assert [job["id"] for job in Store(path).jobs(["match"])] == ["1"]


def test_api(world):
    clock, fake, store, service = world
    fake.add("1", "Product Manager", age_seconds=300)
    fake.add("2", "Product Designer", age_seconds=100)
    service.run_cycle()
    with TestClient(create_app(service, store, run_scheduler=False)) as api:
        assert "Illuminate" in api.get("/").text
        body = api.get("/api/jobs").json()
        assert [job["id"] for job in body["jobs"]] == ["1"] and body["jobs"][0]["precise"]
        assert [job["id"] for job in api.get("/api/jobs?labels=no").json()["jobs"]] == ["2"]
        status = api.get("/api/status").json()
        assert status["counts"] == {"match": 1, "maybe": 0, "no": 1}
        assert status["last_run"]["status"] == "ok" and status["version"] == body["version"]
        assert api.post("/api/refresh").json() == {"started": True}


# ---- resume overlap -------------------------------------------------------

from illuminate import scoring
from illuminate.linkedin import parse_detail
from illuminate.resume import ResumeError, extract_text

DETAIL = """<div class="show-more-less-html__markup"><strong>About</strong><br>Own the roadmap.
<ul><li>5+ years in product</li><li>SQL</li></ul></div>
<ul><li class="description__job-criteria-item"><h3 class="description__job-criteria-subheader">
Seniority level</h3><span class="description__job-criteria-text"> Mid-Senior level </span></li></ul>"""


def assessment(score=80, dealbreaker=False, skip=()):
    dims = {
        name: scoring.Dimension(score=score, applicable=name not in skip, note="ok")
        for name in scoring.DIMENSIONS
    }
    return scoring.Assessment(
        **dims, years_required="5+ years", years_candidate="7 years", dealbreaker=dealbreaker,
        dealbreaker_reason="needs a CA" if dealbreaker else None, strengths=["roadmaps"],
        gaps=["SQL"], summary="Good match.",
    )


class FakeScorer:
    def __init__(self):
        self.calls = []
        self.key = True

    def available(self):
        return self.key

    def reset(self):
        pass

    def assess(self, resume_text, job):
        self.calls.append((resume_text, job["id"], job["description"]))
        return assessment(60 if "Owner" in job["title"] else 80)


@pytest.fixture
def scored(world):
    clock, fake, store, service = world
    original = fake.__call__

    def with_details(params):
        if "job_id" in params:
            fake.calls.append(params)
            return (404, "") if params["job_id"] == "gone" else (200, DETAIL)
        return original(params)

    service.client._fetch = with_details
    service.scorer = FakeScorer()
    return clock, fake, store, service


def test_weights_add_up_and_overall_is_a_weighted_average():
    assert sum(weight for weight, _, _ in scoring.DIMENSIONS.values()) == 100
    a = assessment(80)
    a.role.score = 40  # weight 25
    assert scoring.overall(a) == 70
    assert scoring.overall(assessment(90, dealbreaker=True)) == scoring.DEALBREAKER_CAP
    # A dimension the posting says nothing about is left out, not counted as zero.
    assert scoring.overall(assessment(80, skip=("domain", "context"))) == 80


def test_parse_detail():
    detail = parse_detail(DETAIL)
    assert "Own the roadmap." in detail.description and "- 5+ years in product" in detail.description
    assert detail.criteria == {"Seniority level": "Mid-Senior level"}
    assert parse_detail("<html>sign in</html>") is None


def test_logo_is_read_from_the_card():
    html = card_html("1", "Product Manager").replace(
        '<div class="base-search-card__info">',
        '<img class="artdeco-entity-image" data-delayed-url="https://media.licdn.com/logo.png">'
        '<div class="base-search-card__info">')
    assert parse_cards(html, 1.0)[0].logo_url == "https://media.licdn.com/logo.png"


def test_resume_text_extraction():
    assert "Product leader" in extract_text("cv.txt", ("Product leader. " * 30).encode())
    with pytest.raises(ResumeError):
        extract_text("cv.txt", b"too short")
    with pytest.raises(ResumeError):
        extract_text("cv.pages", b"x" * 500)


def test_jobs_appear_first_and_are_scored_once(scored):
    clock, fake, store, service = scored
    fake.add("1", "Product Manager", age_seconds=600)
    fake.add("2", "Product Owner", age_seconds=300)
    fake.add("3", "Project Manager", age_seconds=200)
    store.save_resume("cv.pdf", "resume text")
    service.run_cycle()
    # Listed straight away, with the overlap still to come.
    assert [(job["id"], job["score_status"]) for job in store.jobs(["match"])] == [("2", None), ("1", None)]
    assert service.score_pending() is False
    assert [(job["id"], job["score"]) for job in store.jobs(["match"])] == [("2", 60), ("1", 80)]
    assert len(service.scorer.calls) == 2  # the project manager is not scored
    assert "Own the roadmap." in service.scorer.calls[0][2]
    # Nothing is scored twice: not on the next pass, nor after the next check.
    service.score_pending()
    clock.now += 900
    service.run_cycle()
    service.score_pending()
    assert len(service.scorer.calls) == 2


def test_new_resume_rescores_everything(scored):
    clock, fake, store, service = scored
    fake.add("1", "Product Manager", age_seconds=600)
    store.save_resume("old.pdf", "old resume")
    service.run_cycle()
    service.score_pending()
    details_fetched = sum("job_id" in call for call in fake.calls)
    store.save_resume("new.pdf", "new resume")
    assert store.jobs(["match"])[0]["score_status"] is None
    service.score_pending()
    assert [call[0] for call in service.scorer.calls] == ["old resume", "new resume"]
    # The job description was kept, so LinkedIn is not asked for it again.
    assert sum("job_id" in call for call in fake.calls) == details_fetched


def test_no_scoring_without_resume_or_key(scored):
    clock, fake, store, service = scored
    fake.add("1", "Product Manager", age_seconds=600)
    service.run_cycle()
    service.score_pending()
    assert service.scorer.calls == []
    store.save_resume("cv.pdf", "resume text")
    service.scorer.key = False
    service.score_pending()
    assert service.scorer.calls == [] and "API key" in service.scoring_note
    assert service.status()["resume"]["pending"] == 1


def test_job_without_a_description_is_marked_not_retried(scored):
    clock, fake, store, service = scored
    fake.add("gone", "Product Manager", age_seconds=600)
    store.save_resume("cv.pdf", "resume text")
    service.run_cycle()
    service.score_pending()
    service.score_pending()
    job = store.jobs(["match"])[0]
    assert job["score_status"] == "failed" and service.scorer.calls == []
    assert sum("job_id" in call for call in fake.calls) == 1


def test_resume_api(scored):
    clock, fake, store, service = scored
    fake.add("1", "Product Manager", age_seconds=600)
    service.run_cycle()
    with TestClient(create_app(service, store, run_scheduler=False)) as api:
        assert api.get("/api/status").json()["resume"] is None
        bad = api.post("/api/resume", content=b"hi", headers={"X-Filename": "cv.txt"})
        assert bad.status_code == 400 and "text" in bad.json()["error"]
        good = api.post("/api/resume", content=b"Product leader. " * 30, headers={"X-Filename": "my%20cv.txt"})
        assert good.json()["filename"] == "my cv.txt"
        assert api.get("/api/jobs").json()["jobs"][0]["overlap"] is None
        service.score_pending()
        overlap = api.get("/api/jobs").json()["jobs"][0]["overlap"]
        assert overlap["score"] == 80 and overlap["years_required"] == "5+ years"
        assert len(overlap["dimensions"]) == 6
        assert api.get("/api/status").json()["resume"]["done"] == 1
        api.delete("/api/resume")
        assert api.get("/api/status").json()["resume"] is None


# ---- scheduling -----------------------------------------------------------


def test_overdue_check_starts_after_the_computer_sleeps(world, monkeypatch):
    """The wait follows the wall clock, so hours asleep do not delay the next check."""
    clock, fake, store, service = world
    monkeypatch.setattr("illuminate.service.WAKE_CHECK_SECONDS", 0.001)
    service.next_run_at = clock.now + 15 * 60
    waits = []
    original = service._wake.wait

    def wait(seconds):
        waits.append(seconds)
        if len(waits) == 3:
            clock.now += 8 * 3600  # the lid is closed for eight hours
        return original(seconds)

    service._wake.wait = wait
    service.wait_for_next_run()
    assert len(waits) == 3  # returned as soon as the clock showed the check was due


def test_a_crashed_check_is_recorded_as_an_error(world):
    clock, fake, store, service = world
    service.run_cycle()

    def broken(*args, **kwargs):
        raise RuntimeError("disk full")

    store.upsert = broken
    fake.add("1", "Product Manager", age_seconds=60)
    clock.now += 900
    with pytest.raises(RuntimeError):
        service.run_cycle()
    last = store.last_run()
    assert last["status"] == "error" and "disk full" in last["note"]
    assert last["finished_at"] >= clock.now - 5


def test_interrupted_check_gets_a_time(tmp_path):
    path = tmp_path / "jobs.db"
    store = Store(path)
    store.start_run(1000.0, 3600)
    store.close()
    last = Store(path).last_run()
    assert last["status"] == "partial" and last["finished_at"] == 1000.0


def test_logo_placeholder_is_ignored():
    html = card_html("1", "Product Manager").replace(
        '<div class="base-search-card__info">',
        '<img class="artdeco-entity-image" src="https://static.licdn.com/aero-v1/sc/h/ghost">'
        '<div class="base-search-card__info">')
    assert parse_cards(html, 1.0)[0].logo_url is None


def test_logos_are_backfilled_without_a_resume(scored):
    clock, fake, store, service = scored
    fake.add("1", "Product Manager", age_seconds=600)
    fake.add("3", "Project Manager", age_seconds=600)
    service.run_cycle()
    detail_with_logo = DETAIL + '<img class="artdeco-entity-image" data-delayed-url="https://media.licdn.com/l.png">'
    service.client._fetch = lambda params: (200, detail_with_logo)
    assert service.fetch_missing_details() is False
    rows = {job["id"]: job for job in store.jobs(["match", "no"])}
    assert rows["1"]["logo_url"] == "https://media.licdn.com/l.png"
    assert rows["3"]["logo_url"] is None  # other roles are not fetched
    assert store.missing_details() == []


def test_api_key_can_be_saved_from_the_page(scored, tmp_path, monkeypatch):
    clock, fake, store, service = scored
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("OTHER=1\nANTHROPIC_API_KEY=old\n")

    def check(key):
        if key.endswith("bad"):
            raise scoring.ScoringUnavailable("Anthropic did not accept that key.")

    app = create_app(service, store, run_scheduler=False, env_path=env, check_key=check)
    with TestClient(app) as api:
        assert api.post("/api/key", json={"key": "hello"}).status_code == 400
        rejected = api.post("/api/key", json={"key": "sk-ant-bad"})
        assert rejected.status_code == 400 and "did not accept" in rejected.json()["error"]
        assert api.post("/api/key", json={"key": "sk-ant-good"}).json() == {"saved": True}
    assert env.read_text() == "OTHER=1\nANTHROPIC_API_KEY=sk-ant-good\n"
    assert oct(env.stat().st_mode & 0o777) == "0o600"
    import os
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-good"
