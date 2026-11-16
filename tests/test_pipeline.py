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
