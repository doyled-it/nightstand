#!/usr/bin/env python3
"""Record trimmed museum API responses as test fixtures.

The museum helper (``nightstand art fetch``) reads two keyless public APIs:

- The Met Collection API. Search moved to ``/public/collection/v1.1/search``
  on 2026-10-01 (the old ``/v1/search`` now answers 410). Object lookups stay
  on ``/public/collection/v1/objects/{id}``; there is no v1.1 object endpoint.
- The Art Institute of Chicago API and its IIIF image server.

Each recorded response is trimmed to the keys the parser in
``nightstand.sources.museums`` reads, so the committed fixtures stay a few KB.
The JSON is real public museum data; no key or account is involved.

Record the Met fixtures (works from any network):

    uv run python scripts/record_museum_fixtures.py met --query "van gogh"

The AIC image CDN sits behind Cloudflare, which issues a challenge to some VPN
egress IPs. If a direct fetch is blocked from your machine, capture the raw
search JSON from a host that is not behind the VPN and trim it here instead of
fetching live:

    # on the unblocked host, with the AIC-User-Agent header set, GET
    #   https://api.artic.edu/api/v1/artworks/search
    #     ?q=van%20gogh&fields=id,title,artist_title,image_id,is_public_domain
    # and save the body to aic_raw.json, then here:
    uv run python scripts/record_museum_fixtures.py aic --raw aic_raw.json

Otherwise fetch AIC live the same way as Met:

    uv run python scripts/record_museum_fixtures.py aic --query "van gogh"
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx

_MET_SEARCH = "https://collectionapi.metmuseum.org/public/collection/v1.1/search"
_MET_OBJECT = "https://collectionapi.metmuseum.org/public/collection/v1/objects/{id}"
_AIC_SEARCH = "https://api.artic.edu/api/v1/artworks/search"
_AIC_USER_AGENT = "nightstand (https://github.com/doyled-it/nightstand)"
# The Met accepts this identifying User-Agent; it is what the helper sends.
_MET_USER_AGENT = "nightstand ( https://github.com/doyled-it/nightstand )"
_TIMEOUT = 30.0

# How many search hits and objects to keep in each fixture.
_KEEP = 3


def trim_met_search(search: dict) -> dict:
    """Keep the real total and the first few object ids."""
    return {
        "total": search.get("total"),
        "objectIDs": list(search.get("objectIDs") or [])[:_KEEP],
    }


def trim_met_object(obj: dict) -> dict:
    """Reduce a Met object to the keys the parser reads."""
    return {
        "objectID": obj.get("objectID"),
        "isPublicDomain": obj.get("isPublicDomain"),
        "primaryImage": obj.get("primaryImage"),
        "title": obj.get("title"),
        "artistDisplayName": obj.get("artistDisplayName"),
        "objectURL": obj.get("objectURL"),
    }


def trim_aic_search(search: dict) -> dict:
    """Reduce an AIC search to the data fields the parser reads."""
    data = search.get("data") or []
    kept = [
        {
            "id": entry.get("id"),
            "title": entry.get("title"),
            "artist_title": entry.get("artist_title"),
            "image_id": entry.get("image_id"),
            "is_public_domain": entry.get("is_public_domain"),
        }
        for entry in data[:_KEEP]
    ]
    return {"data": kept}


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {path}")


def record_met(client: httpx.Client, query: str, out_dir: Path) -> None:
    headers = {"User-Agent": _MET_USER_AGENT}
    search = client.get(
        _MET_SEARCH,
        params={"hasImages": "true", "q": query},
        headers=headers,
        timeout=_TIMEOUT,
    )
    search.raise_for_status()
    search_json = search.json()
    _write(out_dir / "search.json", trim_met_search(search_json))

    for object_id in (search_json.get("objectIDs") or [])[:_KEEP]:
        obj = client.get(
            _MET_OBJECT.format(id=object_id), headers=headers, timeout=_TIMEOUT
        )
        obj.raise_for_status()
        _write(out_dir / f"object_{object_id}.json", trim_met_object(obj.json()))


def record_aic(
    client: httpx.Client, query: str, out_dir: Path, raw: Path | None
) -> None:
    if raw is not None:
        search_json = json.loads(raw.read_text())
    else:
        response = client.get(
            _AIC_SEARCH,
            params={
                "q": query,
                "fields": "id,title,artist_title,image_id,is_public_domain",
                "limit": 10,
            },
            headers={"AIC-User-Agent": _AIC_USER_AGENT},
            timeout=_TIMEOUT,
        )
        response.raise_for_status()
        search_json = response.json()
    _write(out_dir / "search.json", trim_aic_search(search_json))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", choices=("met", "aic"))
    parser.add_argument("--query", default="van gogh", help="search text")
    parser.add_argument("--out-dir", type=Path, help="fixture directory")
    parser.add_argument(
        "--raw",
        type=Path,
        help="AIC only: trim this already-captured raw search JSON instead of fetching",
    )
    args = parser.parse_args(argv)

    out_dir = args.out_dir or Path("tests/fixtures") / args.source
    with httpx.Client() as client:
        if args.source == "met":
            record_met(client, args.query, out_dir)
        else:
            record_aic(client, args.query, out_dir, args.raw)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
