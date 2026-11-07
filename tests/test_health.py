from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx

from nightstand.sources.health import fetch_health, status_problem

NOW = datetime(2026, 11, 6, 21, 15, tzinfo=UTC)


def _status(**overrides) -> dict:
    status = {
        "host": "office",
        "repo": "snaphouse",
        "commit": "abc123",
        "main_commit": "abc123",
        "last_good": "abc123",
        "verify": "pass",
        "checked_at": "2026-11-06T21:15:00-08:00",
    }
    status.update(overrides)
    return status


class _Response:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _Client:
    """A fake httpx client that maps each URL to a payload or an exception."""

    def __init__(self, routes: dict[str, object]) -> None:
        self._routes = routes

    def get(self, url: str, timeout: float | None = None):
        result = self._routes[url]
        if isinstance(result, Exception):
            raise result
        return _Response(result)


def test_status_problem_healthy_is_none():
    assert status_problem(_status()) is None


def test_status_problem_accepts_empty_last_good():
    # A machine publishes last_good == "" before its first successful verify;
    # that is not itself a problem.
    assert status_problem(_status(last_good="")) is None


def test_status_problem_verify_fail():
    assert status_problem(_status(verify="fail")) is not None


def test_status_problem_behind_main():
    assert status_problem(_status(commit="old", main_commit="new")) is not None


def test_fetch_health_all_healthy_has_no_problems():
    url = "http://office.local/status.json"
    client = _Client({url: _status()})
    unreachable: dict[str, datetime] = {}

    problems = fetch_health(client, (url,), NOW, unreachable)

    assert problems == ()
    assert unreachable == {}


def test_fetch_health_reports_unhealthy_machine():
    url = "http://office.local/status.json"
    client = _Client({url: _status(verify="fail")})

    problems = fetch_health(client, (url,), NOW, {})

    assert len(problems) == 1
    assert "office.local" in problems[0]


def test_fetch_health_unreachable_under_15_minutes_is_not_a_problem():
    url = "http://nightstand.local/status.json"
    client = _Client({url: httpx.ConnectError("down")})
    unreachable: dict[str, datetime] = {}

    problems = fetch_health(client, (url,), NOW, unreachable)

    assert problems == ()
    # The first unreachable moment is recorded so the grace can be measured.
    assert unreachable[url] == NOW


def test_fetch_health_unreachable_over_15_minutes_is_a_problem():
    url = "http://nightstand.local/status.json"
    client = _Client({url: httpx.ConnectError("down")})
    unreachable = {url: NOW - timedelta(minutes=16)}

    problems = fetch_health(client, (url,), NOW, unreachable)

    assert len(problems) == 1
    assert "nightstand.local" in problems[0]


def test_fetch_health_reachable_but_not_an_object_is_a_problem():
    # A machine serving valid JSON that is not a status object (an array, say)
    # must be reported, not crash the poll on ``status.get``.
    url = "http://office.local/status.json"
    client = _Client({url: ["not", "a", "status"]})

    problems = fetch_health(client, (url,), NOW, {})

    assert len(problems) == 1
    assert "office.local" in problems[0]


def test_fetch_health_clears_unreachable_on_recovery():
    url = "http://office.local/status.json"
    client = _Client({url: _status()})
    unreachable = {url: NOW - timedelta(minutes=30)}

    problems = fetch_health(client, (url,), NOW, unreachable)

    assert problems == ()
    assert url not in unreachable
