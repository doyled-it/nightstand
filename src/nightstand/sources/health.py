"""System-health polling for the corner marker.

Every machine in the house serves ``http://<host>.local/status.json`` with the
shared schema (see the hub's ``docs/cross-repo-features.md``). ``fetch_health``
polls each URL and returns a short problem string for every machine that is
unhealthy, so the app can show the marker.

A machine is healthy when ``verify == "pass"`` and ``commit == main_commit``;
``last_good`` may be empty, which a machine publishes before its first
successful verify. A machine that cannot be reached is only counted as a
problem once it has been unreachable for over fifteen minutes, so a brief blip
or a reboot does not flash the marker. The caller owns the per-URL
``unreachable_since`` map so that grace survives across polls.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 5.0
UNREACHABLE_GRACE = timedelta(minutes=15)


def host_label(url: str) -> str:
    """The hostname from ``url``, for a human-readable problem string."""
    return urlparse(url).hostname or url


def status_problem(status: dict) -> str | None:
    """A short problem description for one machine's status, or ``None``.

    Healthy is ``verify == "pass"`` and ``commit == main_commit``. ``last_good``
    is not consulted: a machine publishes it empty before its first successful
    verify, and that alone is not a problem.
    """
    if status.get("verify") != "pass":
        return "verify failed"
    if status.get("commit") != status.get("main_commit"):
        return "behind main"
    return None


def fetch_health(
    client: httpx.Client,
    urls: tuple[str, ...],
    now: datetime,
    unreachable_since: dict[str, datetime],
) -> tuple[str, ...]:
    """Poll each status URL and return a problem string per unhealthy machine.

    ``unreachable_since`` is read and updated in place: a URL's first
    unreachable moment is recorded, cleared on recovery, and only promoted to a
    problem once the outage passes ``UNREACHABLE_GRACE``.
    """
    problems: list[str] = []
    for url in urls:
        host = host_label(url)
        try:
            response = client.get(url, timeout=_TIMEOUT)
            response.raise_for_status()
            status = response.json()
        except (httpx.HTTPError, ValueError):
            first = unreachable_since.setdefault(url, now)
            if now - first > UNREACHABLE_GRACE:
                problems.append(f"{host} unreachable")
            continue
        unreachable_since.pop(url, None)
        problem = status_problem(status)
        if problem is not None:
            problems.append(f"{host} {problem}")
    return tuple(problems)
