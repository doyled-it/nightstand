#!/usr/bin/env python3
"""Record MLB Stats API feed snapshots as test fixtures.

The game screen parses the live feed at ``/api/v1.1/game/{pk}/feed/live``. The
same endpoint serves a historical snapshot when given a ``timecode``, so a past
game can be replayed frame by frame. This script pulls a named set of snapshots
from one past game and writes them as fixture JSON.

The full feed is large (hundreds of KB: boxscore, every player, every pitch).
Only a small subset drives the screen, so each snapshot is trimmed to the keys
``parse_game`` and ``summarize`` read, keeping the committed fixtures a few KB.
Pass ``--no-trim`` to keep the whole feed. The recorded JSON is public MLB data.

List the game's available timecodes with ``--list`` to pick snapshots:

    uv run python scripts/record_mlb_fixtures.py --game-pk 823401 --list

Then record them (an empty timecode records the current/no-timecode feed, which
for a finished game is the final):

    uv run python scripts/record_mlb_fixtures.py --game-pk 823401 \
        --out-dir tests/fixtures/mlb \
        --snapshot feed_preview=20260406_200432 \
        --snapshot feed_final=
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx

_FEED = "https://statsapi.mlb.com/api/v1.1/game/{pk}/feed/live"
_TIMESTAMPS = "https://statsapi.mlb.com/api/v1.1/game/{pk}/feed/live/timestamps"
_TIMEOUT = 30.0


def fetch_feed(client: httpx.Client, game_pk: int, timecode: str | None) -> dict:
    """Fetch the feed for ``game_pk``, at ``timecode`` when given."""
    params = {"timecode": timecode} if timecode else None
    response = client.get(_FEED.format(pk=game_pk), params=params, timeout=_TIMEOUT)
    response.raise_for_status()
    return response.json()


def list_timestamps(client: httpx.Client, game_pk: int) -> list[str]:
    """The timecodes the game exposes, oldest first."""
    response = client.get(_TIMESTAMPS.format(pk=game_pk), timeout=_TIMEOUT)
    response.raise_for_status()
    return response.json()


def trim_feed(feed: dict) -> dict:
    """Reduce a feed to the keys the game screen reads.

    Keeps the game id, status, datetime, both teams' id and abbreviation, the
    linescore the summary needs, and every play trimmed to its description and
    half-inning. Play indices are preserved so ``scoringPlays`` still lines up
    with ``allPlays``.
    """
    game_data = feed.get("gameData", {})
    live_data = feed.get("liveData", {})
    linescore = live_data.get("linescore", {})
    plays = live_data.get("plays", {})

    teams = game_data.get("teams", {})

    def team(side: str) -> dict:
        info = teams.get(side, {})
        return {"id": info.get("id"), "abbreviation": info.get("abbreviation")}

    def inning(entry: dict) -> dict:
        return {
            "num": entry.get("num"),
            "away": {"runs": entry.get("away", {}).get("runs")},
            "home": {"runs": entry.get("home", {}).get("runs")},
        }

    def play(entry: dict) -> dict:
        about = entry.get("about", {})
        return {
            "result": {"description": entry.get("result", {}).get("description")},
            "about": {
                "inning": about.get("inning"),
                "halfInning": about.get("halfInning"),
                "endTime": about.get("endTime"),
                "isComplete": about.get("isComplete"),
            },
        }

    line_teams = linescore.get("teams", {})
    return {
        "gamePk": feed.get("gamePk"),
        "gameData": {
            "status": {
                "abstractGameState": game_data.get("status", {}).get(
                    "abstractGameState"
                ),
                "detailedState": game_data.get("status", {}).get("detailedState"),
            },
            "datetime": {
                "dateTime": game_data.get("datetime", {}).get("dateTime"),
            },
            "teams": {"away": team("away"), "home": team("home")},
        },
        "liveData": {
            "linescore": {
                "currentInning": linescore.get("currentInning"),
                "inningState": linescore.get("inningState"),
                "teams": {
                    "away": {"runs": line_teams.get("away", {}).get("runs")},
                    "home": {"runs": line_teams.get("home", {}).get("runs")},
                },
                "innings": [inning(i) for i in linescore.get("innings", [])],
            },
            "plays": {
                "scoringPlays": plays.get("scoringPlays", []),
                "allPlays": [play(p) for p in plays.get("allPlays", [])],
            },
        },
    }


def _parse_snapshot(arg: str) -> tuple[str, str | None]:
    name, _, timecode = arg.partition("=")
    return name, (timecode or None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-pk", type=int, required=True)
    parser.add_argument("--out-dir", type=Path, default=Path("tests/fixtures/mlb"))
    parser.add_argument(
        "--snapshot",
        action="append",
        default=[],
        metavar="NAME=TIMECODE",
        help="fixture name and timecode; an empty timecode records the final",
    )
    parser.add_argument(
        "--list", action="store_true", help="list the game's timecodes and exit"
    )
    parser.add_argument(
        "--no-trim", action="store_true", help="save the whole feed, untrimmed"
    )
    args = parser.parse_args(argv)

    with httpx.Client() as client:
        if args.list:
            for stamp in list_timestamps(client, args.game_pk):
                print(stamp)
            return 0

        args.out_dir.mkdir(parents=True, exist_ok=True)
        for raw in args.snapshot:
            name, timecode = _parse_snapshot(raw)
            feed = fetch_feed(client, args.game_pk, timecode)
            if not args.no_trim:
                feed = trim_feed(feed)
            path = args.out_dir / f"{name}.json"
            path.write_text(json.dumps(feed, indent=2, ensure_ascii=False) + "\n")
            print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
