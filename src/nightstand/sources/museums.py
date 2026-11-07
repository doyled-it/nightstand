"""Gather public-domain art from museum open-access APIs into the art share.

This is a hand-run helper, not part of the display loop: the owner points it at
the mounted art share and it downloads public-domain works with their captions.
It never writes into the repo, and it writes nothing identifying in a request.

Two sources are supported:

- ``met``: the Metropolitan Museum of Art Collection API. Search returns object
  ids; each object is fetched and kept only when ``isPublicDomain`` is true and
  it has a ``primaryImage``.
- ``aic``: the Art Institute of Chicago API. Search returns the fields inline;
  an item is kept only when ``is_public_domain`` is true and it has an
  ``image_id``, and its full-size image comes from the IIIF endpoint.

Each kept item is written as ``<source>-<id>.jpg`` with a sidecar
``<source>-<id>.jpg.txt`` caption (line 1 title, line 2 artist), the same format
the art library reads. An item whose image file already exists is skipped, so a
rerun adds only what is new.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

from nightstand.sources.coverart import USER_AGENT

logger = logging.getLogger(__name__)

_TIMEOUT = 30.0
_DEFAULT_LIMIT = 20

_MET_SEARCH = "https://collectionapi.metmuseum.org/public/collection/v1/search"
_MET_OBJECT = "https://collectionapi.metmuseum.org/public/collection/v1/objects/{id}"
_AIC_SEARCH = "https://api.artic.edu/api/v1/artworks/search"
_AIC_IMAGE = "https://www.artic.edu/iiif/2/{image_id}/full/843,/0/default.jpg"


@dataclass(frozen=True)
class FetchResult:
    """How many images the fetch wrote, and how many it skipped as present."""

    written: int = 0
    skipped: int = 0


@dataclass(frozen=True)
class _Item:
    """One kept work: its output id, image URL, and caption."""

    object_id: int
    image_url: str
    title: str
    artist: str


def fetch_museum(
    client: httpx.Client,
    source: str,
    query: str,
    dest: Path,
    limit: int = _DEFAULT_LIMIT,
) -> FetchResult:
    """Fetch up to ``limit`` public-domain works for ``query`` into ``dest``."""
    dest = Path(dest)
    if source == "met":
        items = _met_items(client, query, limit)
    elif source == "aic":
        items = _aic_items(client, query, limit)
    else:
        raise ValueError(f"unknown museum source {source!r}; use 'met' or 'aic'")

    dest.mkdir(parents=True, exist_ok=True)
    written = 0
    skipped = 0
    for item in items:
        image_path = dest / f"{source}-{item.object_id}.jpg"
        if image_path.exists():
            skipped += 1
            continue
        if _download(client, item.image_url, image_path):
            _write_caption(image_path, item.title, item.artist)
            written += 1
    return FetchResult(written=written, skipped=skipped)


def _met_items(client: httpx.Client, query: str, limit: int) -> list[_Item]:
    search = _get_json(client, _MET_SEARCH, {"hasImages": "true", "q": query})
    object_ids = (search or {}).get("objectIDs") or []
    items: list[_Item] = []
    for object_id in object_ids:
        if len(items) >= limit:
            break
        obj = _get_json(client, _MET_OBJECT.format(id=object_id), None)
        if obj is None:
            continue
        image = obj.get("primaryImage")
        if not obj.get("isPublicDomain") or not image:
            continue
        items.append(
            _Item(
                object_id=int(obj.get("objectID", object_id)),
                image_url=image,
                title=str(obj.get("title") or ""),
                artist=str(obj.get("artistDisplayName") or ""),
            )
        )
    return items


def _aic_items(client: httpx.Client, query: str, limit: int) -> list[_Item]:
    search = _get_json(
        client,
        _AIC_SEARCH,
        {
            "q": query,
            "fields": "id,title,artist_title,image_id,is_public_domain",
            "limit": limit,
        },
    )
    data = (search or {}).get("data") or []
    items: list[_Item] = []
    for entry in data:
        if len(items) >= limit:
            break
        image_id = entry.get("image_id")
        if not entry.get("is_public_domain") or not image_id:
            continue
        items.append(
            _Item(
                object_id=int(entry["id"]),
                image_url=_AIC_IMAGE.format(image_id=image_id),
                title=str(entry.get("title") or ""),
                artist=str(entry.get("artist_title") or ""),
            )
        )
    return items


def _get_json(client: httpx.Client, url: str, params: dict | None) -> dict | None:
    """GET ``url`` and return the parsed JSON object, or ``None`` on any failure."""
    try:
        response = client.get(
            url,
            params=params,
            headers={"User-Agent": USER_AGENT},
            timeout=_TIMEOUT,
        )
    except httpx.HTTPError as error:
        logger.warning("museum request failed for %s: %s", url, error)
        return None
    if response.status_code != 200:
        logger.warning("museum request to %s returned %s", url, response.status_code)
        return None
    try:
        data = response.json()
    except ValueError as error:
        logger.warning("museum response from %s was not JSON: %s", url, error)
        return None
    return data if isinstance(data, dict) else None


def _download(client: httpx.Client, url: str, dest: Path) -> bool:
    """Stream ``url`` to a temp file and rename it to ``dest``; skip on failure."""
    tmp = dest.with_name(dest.name + ".part")
    try:
        with client.stream(
            "GET", url, headers={"User-Agent": USER_AGENT}, timeout=_TIMEOUT
        ) as response:
            if response.status_code != 200:
                logger.warning("image %s returned %s", url, response.status_code)
                return False
            with tmp.open("wb") as handle:
                for chunk in response.iter_bytes():
                    handle.write(chunk)
    except httpx.HTTPError as error:
        logger.warning("image download failed for %s: %s", url, error)
        _unlink_quietly(tmp)
        return False
    tmp.replace(dest)
    return True


def _write_caption(image_path: Path, title: str, artist: str) -> None:
    sidecar = image_path.with_name(image_path.name + ".txt")
    lines = [title] + ([artist] if artist else [])
    sidecar.write_text("\n".join(lines) + "\n")


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
