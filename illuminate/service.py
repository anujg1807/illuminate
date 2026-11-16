"""The background worker: search LinkedIn on a schedule and keep the job list current."""

from __future__ import annotations

import logging
import random
import threading
import time

from . import timing
from .classifier import NO, classify
from .config import Config
from . import scoring
from .linkedin import LinkedInClient, LinkedInError, RateLimited
from .store import Store

log = logging.getLogger("illuminate")

# Each run searches back to the start of the last complete run, plus this much
# overlap, so a job posted while a run was in progress is never missed.
OVERLAP_SECONDS = 15 * 60
# The furthest back a catch-up run reaches after the app has been off for a while.
MAX_CATCH_UP_SECONDS = 7 * 86400
# The full scan reads every job posted in the location, so it only looks this
# far back; anything older is left to the keyword searches.
FULL_SCAN_MAX_SECONDS = 3600


class Service:
    def __init__(
        self, config: Config, store: Store, client: LinkedInClient | None = None, scorer=None
    ) -> None:
        self.config = config
        self.store = store
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._run_lock = threading.Lock()
        self.client = client or LinkedInClient(
            min_delay=config.min_delay_seconds,
            max_delay=config.max_delay_seconds,
            stop=self._stop,
        )
        self._thread: threading.Thread | None = None
        self.scorer = scorer or scoring.Scorer(config.scoring_model)
        self._score_wake = threading.Event()
        self._score_thread: threading.Thread | None = None
        self.scoring_note = ""  # why scoring is paused, if it is
        self.running = False
        self.phase = ""
        self.next_run_at: float | None = None

    # ---- scheduling -------------------------------------------------------

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="illuminate", daemon=True)
            self._thread.start()
            self._score_thread = threading.Thread(
                target=self._score_loop, name="illuminate-scoring", daemon=True
            )
            self._score_thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._score_wake.set()

    def trigger(self) -> bool:
        """Ask for a run right now. Returns False if one is already in progress."""
        if self.running:
            return False
        self._wake.set()
        return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            started = time.time()
            try:
                self.run_cycle()
            except Exception:
                log.exception("run failed")
            interval = self.config.interval_minutes * 60
            self.next_run_at = max(started + interval, time.time() + 5) + random.uniform(0, 20)
            self._wake.clear()
            self._wake.wait(max(0.0, self.next_run_at - time.time()))

    # ---- one run ----------------------------------------------------------

    def window_seconds(self, now: float) -> int:
        last = self.store.last_complete_run_start()
        if last is None:
            return int(self.config.backfill_hours * 3600)
        return int(min(max(now - last, 0) + OVERLAP_SECONDS, MAX_CATCH_UP_SECONDS))

    def run_cycle(self) -> dict:
        with self._run_lock:
            self.running = True
            try:
                return self._run_cycle()
            finally:
                self.running = False
                self.phase = ""
                self._score_wake.set()  # new jobs may be waiting for a score

    def _run_cycle(self) -> dict:
        now = time.time()
        window = self.window_seconds(now)
        run_id = self.store.start_run(now, window)
        requests_before = self.client.requests_made
        found: set[str] = set()
        scanned: set[str] = set()
        new_jobs = new_relevant = 0
        notes: list[str] = []
        complete = True

        # Keyword searches first (quick), then the full scan of every new job.
        # Jobs from a keyword search are all stored, so near-misses can be
        # reviewed under "Other roles"; the full scan keeps product roles only.
        searches = [(query, query, window) for query in self.config.queries]
        if self.config.full_scan:
            searches.append(("", "full scan", min(window, FULL_SCAN_MAX_SECONDS)))

        for keywords, name, search_window in searches:
            self.phase = (
                f"Searching LinkedIn for “{keywords}”" if keywords
                else f"Reading every new job in {self.config.location}"
            )
            result = self.client.search(keywords, self.config.location, search_window)
            for card in result.cards:
                scanned.add(card.id)
                verdict = classify(card.title, card.company)
                if not keywords and verdict.label == NO:
                    continue
                # What LinkedIn's label says, tightened by the fact that the
                # job was returned for this window. If the two ever disagree,
                # believe the label.
                from_label = timing.bounds_from_label(card.posted_label, card.seen_at)
                from_window = timing.bounds_from_window(result.window_seconds, card.seen_at)
                bounds = timing.intersect(from_label, from_window) or from_label or from_window
                is_new = self.store.upsert(card, verdict, bounds, name)
                found.add(card.id)
                if is_new:
                    new_jobs += 1
                    new_relevant += verdict.label != NO
                    self._score_wake.set()  # score it now, without waiting for the check to end
            if result.note:
                notes.append(result.note)
            if not result.complete:
                complete = False
                break  # LinkedIn is pushing back: stop and try again next time
            if self._stop.is_set():
                complete = False
                break

        summary = {
            "requests": self.client.requests_made - requests_before,
            "scanned": len(scanned),
            "found": len(found),
            "new_jobs": new_jobs,
            "new_relevant": new_relevant,
            "note": "; ".join(notes),
        }
        status = "ok" if complete else "partial"
        self.store.finish_run(run_id, time.time(), status, **summary)
        log.info(
            "check %s: looked back %d min, read %d job titles, %d new product roles, %d requests%s",
            status, window // 60, len(scanned), new_relevant, summary["requests"],
            f" - {summary['note']}" if summary["note"] else "",
        )

        if complete:
            self.phase = "Pinning down exact posting times"
            try:
                self.refine(self.config.refine_requests_per_cycle)
            except LinkedInError as error:
                log.info("stopped refining posting times: %s", error)
        return {"status": status, **summary}

    # ---- exact posting times ---------------------------------------------

    def refine(self, budget: int) -> int:
        """Narrow down the posting time of jobs first seen when already old.

        LinkedIn's "posted in the last W seconds" filter is exact, so asking
        whether a job appears for different values of W brackets its posting
        time. Each question halves the uncertainty. Returns the number of
        requests spent.
        """
        spent_before = self.client.requests_made

        def left() -> int:
            return budget - (self.client.requests_made - spent_before)

        for job in self.store.refine_candidates():
            if left() < 2 or self._stop.is_set():
                break
            self._refine_job(job, left)
        return self.client.requests_made - spent_before

    def _is_listed(self, job: dict, keywords: str, window: int) -> tuple[bool, float]:
        cards = self.client.search_page(keywords, self.config.location, window)
        seen_at = cards[0].seen_at if cards else time.time()
        return any(card.id == job["id"] for card in cards), seen_at

    @staticmethod
    def _window_back_to(moment: float) -> int:
        """A window reaching back to `moment` that LinkedIn answers live."""
        window = int(time.time() - moment)
        return window + 1 if window % 60 == 0 else window

    def _refine_job(self, job: dict, left) -> None:
        lo, hi = job["posted_lo"], job["posted_hi"]
        # First find a narrow search that returns this job at all, using a
        # window that certainly contains it. Without one, "not listed" answers
        # would mean nothing. LinkedIn's keyword matching is quirky, so try the
        # title with the company, then the company alone.
        for keywords in (f"{job['title']} {job['company']}", job["company"]):
            if not keywords.strip() or left() < 1:
                continue
            listed, _ = self._is_listed(job, keywords, self._window_back_to(lo - 120))
            if listed:
                break
        else:
            self.store.set_refine_failed(job["id"])
            return
        while hi - lo > timing.PRECISE_SECONDS and left() >= 1:
            window = self._window_back_to((lo + hi) / 2)
            listed, asked_at = self._is_listed(job, keywords, window)
            cutoff = asked_at - window
            if listed:
                lo = max(lo, cutoff - timing.SLACK)
            else:
                hi = min(hi, cutoff + timing.SLACK)
            if lo > hi:  # contradictory answers: keep what we had
                return
            self.store.set_bounds(job["id"], (lo, hi))

    # ---- resume overlap ----------------------------------------------------

    def resume_changed(self) -> None:
        self.scoring_note = ""
        self._score_wake.set()

    def _score_loop(self) -> None:
        """Runs beside the checker, so a new job appears at once and its score follows."""
        while not self._stop.is_set():
            self._score_wake.clear()
            try:
                more = self.score_pending()
            except Exception:
                log.exception("scoring failed")
                more = False
            if not more:
                # Wake on a new job or resume; otherwise look again in five minutes
                # (which also retries after a paused state such as a rate limit).
                self._score_wake.wait(300)

    def score_pending(self, limit: int = 20) -> bool:
        """Score jobs that have no overlap yet. Returns True if more may remain."""
        resume = self.store.resume()
        if not resume:
            self.scoring_note = ""
            return False
        if not self.scorer.available():
            self.scoring_note = "Add your Anthropic API key to turn on Resume Overlap (see the README)."
            return False
        jobs = self.store.unscored(resume["id"], limit)
        for job in jobs:
            if self._stop.is_set():
                return False
            current = self.store.resume()
            if not current or current["id"] != resume["id"]:
                return True  # the resume was replaced: start over with the new one
            try:
                self._score_job(job, resume)
                self.scoring_note = ""
            except scoring.ScoringUnavailable as error:
                self.scoring_note = str(error)
                log.info("resume overlap paused: %s", error)
                return False
            except RateLimited:
                self.scoring_note = "LinkedIn is limiting requests; Resume Overlap will continue shortly."
                return False
            except LinkedInError:
                return False  # the app is stopping
            except Exception as error:
                log.exception("could not score job %s", job["id"])
                self.store.save_score(job["id"], resume["id"], None, None, f"Scoring failed: {error}")
        return len(jobs) == limit

    def _score_job(self, job: dict, resume: dict) -> None:
        if not job["description"] and not job["detail_failed"]:
            detail = self.client.job_detail(job["id"])
            self.store.save_detail(
                job["id"], detail.description if detail else None,
                detail.criteria if detail else None, detail.logo_url if detail else None,
            )
            job["description"] = detail.description if detail else None
            job["criteria"] = detail.criteria if detail else {}
        if not job["description"]:
            self.store.save_score(
                job["id"], resume["id"], None, None,
                "LinkedIn no longer shows this job's description.",
            )
            return
        assessment = self.scorer.assess(resume["text"], job)
        self.store.save_score(
            job["id"], resume["id"], scoring.overall(assessment), scoring.to_detail(assessment)
        )

    # ---- status -----------------------------------------------------------

    def status(self) -> dict:
        return {
            "running": self.running,
            "phase": self.phase,
            "next_run_at": None if self.running else self.next_run_at,
            "interval_minutes": self.config.interval_minutes,
            "last_run": self.store.last_run(),
            "counts": self.store.counts(),
            "imprecise": self.store.imprecise_count(),
            "version": self.store.version,
            "location": self.config.location,
            "resume": self._resume_status(),
        }

    def _resume_status(self) -> dict | None:
        resume = self.store.resume()
        if not resume:
            return None
        return {
            "filename": resume["filename"],
            "uploaded_at": resume["uploaded_at"],
            "note": self.scoring_note,
            "has_key": self.scorer.available(),
            **self.store.score_counts(),
        }
