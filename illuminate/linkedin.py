"""Read LinkedIn's public (logged-out) job search.

The approach follows the open-source JobSpy project (MIT licence): LinkedIn
serves search results to visitors who are not signed in, ten at a time, from
the `seeMoreJobPostings` endpoint. No LinkedIn account or login is involved.

Unlike JobSpy this client keeps the "49 minutes ago" label of each result,
accepts time windows in seconds, paces every request and backs off when
LinkedIn answers "too many requests".
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from bs4 import BeautifulSoup

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
PAGE_SIZE = 10
# LinkedIn never returns more than about 1000 results for one search.
RESULT_CAP = 1000
HEADERS = {"accept-language": "en-US,en;q=0.9"}


def uncached_window(seconds: float) -> int:
    """A time window LinkedIn will answer live rather than from its cache.

    LinkedIn caches the answers for common windows such as exactly 30 or 60
    minutes, and a cached answer can be half an hour stale: it lacks the newest
    jobs and includes ones that have since aged out of the window. A window
    that is not a whole number of minutes is computed fresh, so every search
    is made a few seconds longer than asked.
    """
    window = int(seconds) + random.randint(1, 59)
    return window + 1 if window % 60 == 0 else window


class LinkedInError(Exception):
    """LinkedIn could not be read."""


class RateLimited(LinkedInError):
    """LinkedIn keeps answering "too many requests"."""


@dataclass(frozen=True)
class Card:
    """One job as it appears in a page of search results."""

    id: str
    title: str
    company: str
    company_url: str | None
    location: str
    posted_label: str | None
    seen_at: float

    @property
    def url(self) -> str:
        return f"https://www.linkedin.com/jobs/view/{self.id}"


@dataclass
class SearchResult:
    cards: list[Card] = field(default_factory=list)
    # The window actually searched (slightly longer than requested).
    window_seconds: int = 0
    # False when the search stopped early, so some jobs may be missing.
    complete: bool = True
    # True when the 1000-result cap was reached.
    truncated: bool = False
    # True when the search stopped because LinkedIn said "too many requests".
    rate_limited: bool = False
    note: str = ""


def parse_cards(html: str, seen_at: float) -> list[Card]:
    soup = BeautifulSoup(html, "html.parser")
    cards: list[Card] = []
    for node in soup.find_all(class_="base-search-card"):
        job_id = str(node.get("data-entity-urn", "")).split(":")[-1]
        title = node.find(class_="base-search-card__title")
        if not job_id or title is None:
            continue
        company = node.find(class_="base-search-card__subtitle")
        company_link = company.find("a", href=True) if company else None
        location = node.find(class_="job-search-card__location")
        posted = node.find("time")
        cards.append(
            Card(
                id=job_id,
                title=title.get_text(strip=True),
                company=company.get_text(strip=True) if company else "",
                company_url=str(company_link["href"]).split("?")[0] if company_link else None,
                location=location.get_text(strip=True) if location else "",
                posted_label=posted.get_text(strip=True) if posted else None,
                seen_at=seen_at,
            )
        )
    return cards


def _default_fetch() -> Callable[[dict], tuple[int, str]]:
    from curl_cffi import requests

    # curl_cffi presents the same network fingerprint as a real Chrome browser.
    session = requests.Session(impersonate="chrome")

    def fetch(params: dict) -> tuple[int, str]:
        response = session.get(SEARCH_URL, params=params, headers=HEADERS, timeout=30)
        return response.status_code, response.text

    return fetch


class LinkedInClient:
    def __init__(
        self,
        min_delay: float = 3.0,
        max_delay: float = 6.0,
        retry_waits: tuple[float, ...] = (30.0, 90.0),
        fetch: Callable[[dict], tuple[int, str]] | None = None,
        clock: Callable[[], float] = time.time,
        stop: threading.Event | None = None,
    ) -> None:
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.retry_waits = retry_waits
        self._fetch = fetch
        self._clock = clock
        self._stop = stop or threading.Event()
        self._last_request = 0.0
        self.requests_made = 0

    def _sleep(self, seconds: float) -> None:
        if seconds > 0 and self._stop.wait(seconds):
            raise LinkedInError("stopped")

    def _get(self, params: dict) -> tuple[str, float]:
        """One paced request. Returns the page and the time it was read."""
        if self._fetch is None:
            self._fetch = _default_fetch()
        problem = ""
        for wait in (0.0, *self.retry_waits):
            self._sleep(wait)
            gap = random.uniform(self.min_delay, self.max_delay)
            self._sleep(self._last_request + gap - self._clock())
            self._last_request = self._clock()
            self.requests_made += 1
            try:
                status, text = self._fetch(params)
            except LinkedInError:
                raise
            except Exception as error:  # network trouble: retry
                problem = f"network error: {error}"
                continue
            if status == 200:
                return text, self._clock()
            problem = f"LinkedIn answered with status {status}"
            if status != 429 and status < 500:
                raise LinkedInError(problem)
        if "429" in problem:
            raise RateLimited(problem)
        raise LinkedInError(problem)

    def search_page(
        self, keywords: str, location: str, window_seconds: int, start: int = 0
    ) -> list[Card]:
        """One page of results, newest first, limited to the last `window_seconds`."""
        params = {
            "location": location,
            "sortBy": "DD",
            "f_TPR": f"r{int(window_seconds)}",
            "start": start,
        }
        if keywords:  # no keywords means every job in the location
            params["keywords"] = keywords
        text, seen_at = self._get(params)
        cards = parse_cards(text, seen_at)
        if not cards and len(text.strip()) > 200:
            raise LinkedInError("LinkedIn returned an unexpected page (possibly blocked)")
        return cards

    def search(self, keywords: str, location: str, window_seconds: int) -> SearchResult:
        """Every result for a search, following pages until LinkedIn has no more.

        With empty `keywords` this lists every job posted in the location.
        """
        name = f"\"{keywords}\"" if keywords else "full scan"
        window_seconds = uncached_window(window_seconds)
        result = SearchResult(window_seconds=window_seconds)
        seen: set[str] = set()
        start = 0
        last_page_full = False
        try:
            while True:
                if start >= RESULT_CAP:
                    result.truncated = True
                    result.note = (
                        f"{name}: more than {RESULT_CAP} results; "
                        "LinkedIn does not serve the rest"
                    )
                    break
                cards = self.search_page(keywords, location, window_seconds, start)
                if not cards and last_page_full:
                    # A full page followed by nothing: ask once more, because
                    # an empty page can also mean a throttled request.
                    cards = self.search_page(keywords, location, window_seconds, start)
                if not cards:
                    break
                fresh = [card for card in cards if card.id not in seen]
                seen.update(card.id for card in fresh)
                result.cards.extend(fresh)
                if not fresh:
                    break
                last_page_full = len(cards) >= PAGE_SIZE
                start += len(cards)
        except LinkedInError as error:
            result.complete = False
            result.note = f"{name}: {error}"
            result.rate_limited = isinstance(error, RateLimited)
        return result
