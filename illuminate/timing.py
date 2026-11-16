"""Work out when a job was posted.

LinkedIn's public pages never state an exact posting time. They give two clues:

1. A label on each search result such as "49 minutes ago" or "3 hours ago".
2. The search's time-window filter: a job is returned for "posted in the last
   W seconds" only if it really is that recent.

Every clue is turned into a pair of bounds (earliest, latest) on the posting
time, as Unix timestamps. Bounds from repeated sightings are intersected, so
the estimate only ever gets tighter. The posting time shown is the midpoint.
"""

from __future__ import annotations

import re

MINUTE, HOUR, DAY = 60, 3600, 86400
# A job whose bounds are this tight is considered pinned down "to the minute".
PRECISE_SECONDS = 90
# Tolerance for the gap between our clock and the moment LinkedIn evaluated a request.
SLACK = 10

_LABEL = re.compile(r"(\d+)\s+(minute|hour|day|week|month|year)s?\s+ago", re.I)
_UNIT = {"day": DAY, "week": 7 * DAY, "month": 30 * DAY, "year": 365 * DAY}

Bounds = tuple[float, float]


def bounds_from_label(label: str | None, seen_at: float) -> Bounds | None:
    """Bounds implied by a label like "49 minutes ago" read at time `seen_at`."""
    if not label:
        return None
    text = label.strip().lower()
    if "just now" in text or "moments ago" in text:
        return seen_at - MINUTE - SLACK, seen_at
    m = _LABEL.search(text)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    if unit == "minute":
        # Whole minutes, rounded down: "49 minutes ago" means 49:00 to 49:59 old.
        return seen_at - (n + 1) * MINUTE - SLACK, seen_at - n * MINUTE + SLACK
    if unit == "hour":
        # LinkedIn starts saying "1 hour ago" a little before the hour is up,
        # so allow from half an hour early to the end of that hour.
        return seen_at - (n + 1) * HOUR - SLACK, seen_at - (n - 0.5) * HOUR
    size = _UNIT[unit]
    return seen_at - (n + 1) * size - HOUR, seen_at - (n - 0.5) * size


def bounds_from_window(window_seconds: float, seen_at: float) -> Bounds:
    """A job returned for "posted in the last W seconds" is at most W seconds old."""
    return seen_at - window_seconds - SLACK, seen_at + SLACK


def intersect(a: Bounds | None, b: Bounds | None) -> Bounds | None:
    """The overlap of two bounds, or None when they do not overlap."""
    if a is None:
        return b
    if b is None:
        return a
    lo, hi = max(a[0], b[0]), min(a[1], b[1])
    return (lo, hi) if lo <= hi else None


def merge(stored: Bounds, new: Bounds) -> Bounds:
    """Combine what we knew with a new sighting.

    Normally the two overlap and the result is tighter. If the new sighting is
    clearly later than anything we believed, LinkedIn has re-posted the job, so
    the new bounds win. A new sighting that is clearly earlier is ignored.
    """
    both = intersect(stored, new)
    if both is not None:
        return both
    if new[0] > stored[1]:
        return new
    return stored


def midpoint(bounds: Bounds) -> float:
    return (bounds[0] + bounds[1]) / 2


def is_precise(bounds: Bounds) -> bool:
    return bounds[1] - bounds[0] <= PRECISE_SECONDS
