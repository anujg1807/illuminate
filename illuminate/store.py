"""The job list, kept in a single SQLite file so it survives restarts."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from . import timing
from .classifier import Verdict
from .linkedin import Card
from .timing import Bounds

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,   -- LinkedIn's job id
    title         TEXT NOT NULL,
    company       TEXT NOT NULL,
    company_url   TEXT,
    location      TEXT NOT NULL,
    url           TEXT NOT NULL,
    label         TEXT NOT NULL,      -- match / maybe / no
    reason        TEXT NOT NULL,
    posted_lo     REAL NOT NULL,      -- earliest possible posting time (Unix seconds)
    posted_hi     REAL NOT NULL,      -- latest possible posting time
    posted_at     REAL NOT NULL,      -- best estimate: the midpoint
    posted_label  TEXT,               -- LinkedIn's own wording when last seen
    found_by      TEXT NOT NULL,      -- the search phrase that first surfaced it
    first_seen_at REAL NOT NULL,
    last_seen_at  REAL NOT NULL,
    refine_failed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS jobs_posted ON jobs (posted_at DESC);

CREATE TABLE IF NOT EXISTS resumes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    filename    TEXT NOT NULL,
    text        TEXT NOT NULL,
    uploaded_at REAL NOT NULL
);

-- One Resume Overlap result per job per resume, computed once and kept.
CREATE TABLE IF NOT EXISTS scores (
    job_id     TEXT NOT NULL,
    resume_id  INTEGER NOT NULL,
    status     TEXT NOT NULL,         -- done / failed
    score      REAL,                  -- 0 to 100
    detail     TEXT NOT NULL,         -- JSON: the breakdown, or the reason it failed
    created_at REAL NOT NULL,
    PRIMARY KEY (job_id, resume_id)
);

-- Jobs waiting to be graded: the latest 50 when a resume is uploaded, jobs
-- found after that, and any job whose Grade button was clicked (first in line).
CREATE TABLE IF NOT EXISTS grade_queue (
    job_id       TEXT PRIMARY KEY,
    requested_at REAL NOT NULL,
    manual       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at     REAL NOT NULL,
    finished_at    REAL,
    status         TEXT NOT NULL,     -- running / ok / partial / error
    window_seconds REAL NOT NULL,
    requests       INTEGER NOT NULL DEFAULT 0,
    scanned        INTEGER NOT NULL DEFAULT 0,   -- job titles read
    found          INTEGER NOT NULL DEFAULT 0,   -- of those, jobs kept
    new_jobs       INTEGER NOT NULL DEFAULT 0,
    new_relevant   INTEGER NOT NULL DEFAULT 0,
    note           TEXT NOT NULL DEFAULT ''
);
"""


# When a resume is uploaded, the latest this-many product roles are graded.
AUTO_GRADE_LATEST = 50


class Store:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock, self._db:
            self._db.executescript(SCHEMA)
            # Columns added after the first release.
            have = {row["name"] for row in self._db.execute("PRAGMA table_info(jobs)")}
            for column, kind in (
                ("logo_url", "TEXT"),
                ("description", "TEXT"),
                ("criteria", "TEXT"),                       # JSON
                ("detail_failed", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if column not in have:
                    self._db.execute(f"ALTER TABLE jobs ADD COLUMN {column} {kind}")
            # Placeholder images saved by an earlier version are not logos.
            self._db.execute(
                "UPDATE jobs SET logo_url = NULL WHERE logo_url NOT LIKE 'https://media.licdn.com/%'"
            )
            # A run left "running" means the app was closed mid-run.
            self._db.execute(
                "UPDATE runs SET status = 'partial', note = 'interrupted',"
                " finished_at = started_at WHERE status = 'running'"
            )
        # Changes whenever the job list changes; the page watches it to know when to reload.
        self.version = int(time.time() * 1000)

    def _bump(self) -> None:
        self.version += 1

    # ---- jobs -------------------------------------------------------------

    def upsert(self, card: Card, verdict: Verdict, bounds: Bounds, found_by: str) -> bool:
        """Add a job or update a known one. Returns True when the job is new."""
        with self._lock:
            is_new = self._upsert(card, verdict, bounds, found_by)
            if card.logo_url:
                with self._db:
                    self._db.execute(
                        "UPDATE jobs SET logo_url = ? WHERE id = ? AND logo_url IS NOT ?",
                        (card.logo_url, card.id, card.logo_url),
                    )
            return is_new

    def _upsert(self, card: Card, verdict: Verdict, bounds: Bounds, found_by: str) -> bool:
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT posted_lo, posted_hi FROM jobs WHERE id = ?", (card.id,)
            ).fetchone()
            if row is None:
                self._db.execute(
                    "INSERT INTO jobs (id, title, company, company_url, location, url, label,"
                    " reason, posted_lo, posted_hi, posted_at, posted_label, found_by,"
                    " first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        card.id, card.title, card.company, card.company_url, card.location,
                        card.url, verdict.label, verdict.reason, bounds[0], bounds[1],
                        timing.midpoint(bounds), card.posted_label, found_by,
                        card.seen_at, card.seen_at,
                    ),
                )
                self._bump()
                return True
            stored = (row["posted_lo"], row["posted_hi"])
            merged = timing.merge(stored, bounds)
            self._db.execute(
                "UPDATE jobs SET title = ?, company = ?, company_url = ?, location = ?,"
                " label = ?, reason = ?, posted_lo = ?, posted_hi = ?, posted_at = ?,"
                " posted_label = ?, last_seen_at = ? WHERE id = ?",
                (
                    card.title, card.company, card.company_url, card.location,
                    verdict.label, verdict.reason, merged[0], merged[1],
                    timing.midpoint(merged), card.posted_label, card.seen_at, card.id,
                ),
            )
            if merged != stored:
                self._bump()
            return False

    def set_bounds(self, job_id: str, bounds: Bounds) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE jobs SET posted_lo = ?, posted_hi = ?, posted_at = ? WHERE id = ?",
                (bounds[0], bounds[1], timing.midpoint(bounds), job_id),
            )
            self._bump()

    def set_refine_failed(self, job_id: str) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE jobs SET refine_failed = 1 WHERE id = ?", (job_id,))

    def reclassify(self, classify) -> None:
        """Re-apply the title rules to every stored job (the rules may have changed)."""
        with self._lock, self._db:
            rows = self._db.execute("SELECT id, title, company, label, reason FROM jobs").fetchall()
            for row in rows:
                verdict = classify(row["title"], row["company"])
                if (verdict.label, verdict.reason) != (row["label"], row["reason"]):
                    self._db.execute(
                        "UPDATE jobs SET label = ?, reason = ? WHERE id = ?",
                        (verdict.label, verdict.reason, row["id"]),
                    )

    def jobs(self, labels: list[str], limit: int = 3000) -> list[dict]:
        """Jobs with the given labels, most recently posted first.

        Each carries its Resume Overlap, if it has been graded (against
        whichever resume was loaded at the time).
        """
        if not labels:
            return []
        marks = ",".join("?" * len(labels))
        with self._lock:
            rows = self._db.execute(
                "SELECT jobs.id, title, company, company_url, location, url, label, reason,"
                " posted_lo, posted_hi, posted_at, found_by, first_seen_at, refine_failed,"
                " logo_url, scores.status AS score_status, scores.score AS score,"
                " scores.detail AS score_detail, grade_queue.job_id IS NOT NULL AS queued"
                " FROM jobs LEFT JOIN scores ON scores.job_id = jobs.id"
                " LEFT JOIN grade_queue ON grade_queue.job_id = jobs.id"
                f" WHERE label IN ({marks})"
                " ORDER BY posted_at DESC, first_seen_at DESC, jobs.id DESC LIMIT ?",
                (*labels, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._db.execute("SELECT label, COUNT(*) AS n FROM jobs GROUP BY label").fetchall()
        counts = {"match": 0, "maybe": 0, "no": 0}
        counts.update({row["label"]: row["n"] for row in rows})
        return counts

    def refine_candidates(self, limit: int = 50) -> list[dict]:
        """Relevant jobs whose posting time is not yet known to the minute, newest first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM jobs WHERE label IN ('match', 'maybe') AND refine_failed = 0"
                " AND posted_hi - posted_lo > ? ORDER BY posted_at DESC LIMIT ?",
                (timing.PRECISE_SECONDS, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def imprecise_count(self) -> int:
        with self._lock:
            return self._db.execute(
                "SELECT COUNT(*) FROM jobs WHERE label IN ('match', 'maybe')"
                " AND posted_hi - posted_lo > ?",
                (timing.PRECISE_SECONDS,),
            ).fetchone()[0]

    # ---- resume and overlap scores ----------------------------------------

    def save_resume(self, filename: str, text: str) -> int:
        """Store a new resume in place of the previous one.

        Grades are never cleared: a job graded once keeps its grade for good,
        and the new resume is used only for jobs not graded yet. Of those, the
        ones among the latest AUTO_GRADE_LATEST product roles are queued for
        grading; the rest wait for their Grade button.
        """
        with self._lock, self._db:
            latest = self._db.execute(
                "SELECT id FROM jobs WHERE label IN ('match', 'maybe')"
                " ORDER BY posted_at DESC LIMIT ?",
                (AUTO_GRADE_LATEST,),
            ).fetchall()
            now = time.time()
            self._db.executemany(
                "INSERT OR IGNORE INTO grade_queue (job_id, requested_at) VALUES (?, ?)",
                [(row["id"], now) for row in latest],
            )
            cursor = self._db.execute(
                "INSERT INTO resumes (filename, text, uploaded_at) VALUES (?, ?, ?)",
                (filename, text, time.time()),
            )
            self._bump()
            return int(cursor.lastrowid)

    def resume(self) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM resumes WHERE text != '' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def remove_resume(self) -> None:
        """Forget the resume's text. Grades already made are kept."""
        with self._lock, self._db:
            self._db.execute("UPDATE resumes SET text = ''")
            self._bump()

    def queue_grade(self, job_id: str, manual: bool = False) -> None:
        """Ask for a job to be graded (no effect once it has been graded)."""
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO grade_queue (job_id, requested_at, manual) VALUES (?, ?, ?)"
                " ON CONFLICT (job_id) DO UPDATE SET manual = MAX(manual, excluded.manual)",
                (job_id, time.time(), int(manual)),
            )
            self._bump()

    def is_graded(self, job_id: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM scores WHERE job_id = ?", (job_id,)).fetchone() is not None

    def missing_details(self, limit: int = 10) -> list[dict]:
        """Product roles whose full posting (and logo) has not been fetched yet, newest first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id FROM jobs WHERE label IN ('match', 'maybe') AND description IS NULL"
                " AND detail_failed = 0 ORDER BY posted_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def unscored(self, resume_id: int, limit: int = 20) -> list[dict]:
        """Queued jobs not yet graded: clicked ones first, then newest first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT jobs.* FROM grade_queue JOIN jobs ON jobs.id = grade_queue.job_id"
                " LEFT JOIN scores ON scores.job_id = jobs.id WHERE scores.job_id IS NULL"
                " ORDER BY grade_queue.manual DESC, jobs.posted_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        jobs = [dict(row) for row in rows]
        for job in jobs:
            job["criteria"] = json.loads(job["criteria"]) if job["criteria"] else {}
        return jobs

    def score_counts(self) -> dict[str, int]:
        resume = self.resume()
        if not resume:
            return {"done": 0, "pending": 0}
        with self._lock:
            done = self._db.execute(
                "SELECT COUNT(DISTINCT jobs.id) FROM jobs JOIN scores ON scores.job_id = jobs.id"
                " WHERE label IN ('match', 'maybe')"
            ).fetchone()[0]
            queued = self._db.execute(
                "SELECT COUNT(*) FROM grade_queue LEFT JOIN scores ON scores.job_id = grade_queue.job_id"
                " WHERE scores.job_id IS NULL"
            ).fetchone()[0]
        return {"done": done, "pending": queued}

    def save_detail(
        self, job_id: str, description: str | None, criteria: dict | None, logo_url: str | None = None
    ) -> None:
        with self._lock, self._db:
            if logo_url:  # jobs found before logos were collected get theirs here
                self._db.execute(
                    "UPDATE jobs SET logo_url = ? WHERE id = ? AND logo_url IS NULL", (logo_url, job_id)
                )
            self._db.execute(
                "UPDATE jobs SET description = ?, criteria = ?, detail_failed = ? WHERE id = ?",
                (description, json.dumps(criteria or {}), int(description is None), job_id),
            )

    def save_score(
        self, job_id: str, resume_id: int, score: int | None, detail: dict | None, error: str = ""
    ) -> None:
        """Record a job's grade. A job is graded once, ever: a second grade is ignored.

        A failed attempt is recorded too, so it is not retried.
        """
        with self._lock, self._db:
            if self._db.execute("SELECT 1 FROM scores WHERE job_id = ?", (job_id,)).fetchone():
                return
            row = self._db.execute("SELECT filename FROM resumes WHERE id = ?", (resume_id,)).fetchone()
            detail = dict(detail) if detail is not None else {"error": error}
            detail["resume"] = row["filename"] if row else ""
            self._db.execute(
                "INSERT INTO scores (job_id, resume_id, status, score, detail, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    job_id, resume_id, "done" if score is not None else "failed", score,
                    json.dumps(detail), time.time(),
                ),
            )
            self._bump()

    # ---- runs -------------------------------------------------------------

    def start_run(self, started_at: float, window_seconds: float) -> int:
        with self._lock, self._db:
            cursor = self._db.execute(
                "INSERT INTO runs (started_at, status, window_seconds) VALUES (?, 'running', ?)",
                (started_at, window_seconds),
            )
            return int(cursor.lastrowid)

    def finish_run(self, run_id: int, finished_at: float, status: str, **numbers) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE runs SET finished_at = ?, status = ?, requests = ?, scanned = ?,"
                " found = ?, new_jobs = ?, new_relevant = ?, note = ? WHERE id = ?",
                (
                    finished_at, status, numbers.get("requests", 0),
                    numbers.get("scanned", 0), numbers.get("found", 0),
                    numbers.get("new_jobs", 0), numbers.get("new_relevant", 0),
                    numbers.get("note", ""), run_id,
                ),
            )

    def last_complete_run_start(self) -> float | None:
        """When the last fully successful run began; later runs search back to here."""
        with self._lock:
            row = self._db.execute(
                "SELECT MAX(started_at) AS t FROM runs WHERE status = 'ok'"
            ).fetchone()
        return row["t"]

    def last_run(self) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM runs WHERE status != 'running' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def close(self) -> None:
        with self._lock:
            self._db.close()
