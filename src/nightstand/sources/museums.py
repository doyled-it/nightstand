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
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
from PIL import Image, UnidentifiedImageError

from nightstand.sources.coverart import _user_agent

logger = logging.getLogger(__name__)

_TIMEOUT = 30.0
_DEFAULT_LIMIT = 20
# Cap each image so a hostile or broken response cannot stream an unbounded body
# onto the device. Museum originals are large but comfortably under this.
_MAX_BYTES = 64 * 1024 * 1024

_MET_SEARCH = "https://collectionapi.metmuseum.org/public/collection/v1/search"
_MET_OBJECT = "https://collectionapi.metmuseum.org/public/collection/v1/objects/{id}"
_AIC_SEARCH = "https://api.artic.edu/api/v1/artworks/search"
_AIC_IMAGE = "https://www.artic.edu/iiif/2/{image_id}/full/843,/0/default.jpg"

# The URLs in a museum response steer where this helper connects. The Met serves
# its images from one host, and an AIC image_id is a UUID; validate both before
# fetching so a tampered response cannot redirect a request to an internal
# address or inject path segments into the IIIF URL.
_MET_IMAGE_HOST = "images.metmuseum.org"
_AIC_IMAGE_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


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
        if not isinstance(image, str) or not _valid_met_image(image):
            logger.warning(
                "skipping Met object %s: image URL not on the trusted host",
                object_id,
            )
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
        if not isinstance(image_id, str) or not _AIC_IMAGE_ID_RE.match(image_id):
            logger.warning(
                "skipping AIC item %s: image_id is not a UUID", entry.get("id")
            )
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
            headers={"User-Agent": _user_agent()},
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


def _valid_met_image(url: str) -> bool:
    """Whether ``url`` is an https URL on the Met's trusted image host."""
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.hostname == _MET_IMAGE_HOST


def _download(client: httpx.Client, url: str, dest: Path) -> bool:
    """Stream ``url`` to a temp file and rename it to ``dest``; skip on failure.

    The body is capped so a hostile or broken source cannot fill the disk, and it
    is confirmed to decode as an image before it is renamed into place, so only a
    usable image ever lands in the destination.
    """
    tmp = dest.with_name(dest.name + ".part")
    try:
        with client.stream(
            "GET",
            url,
            # Refuse content-encoding so a small compressed body cannot amplify
            # past the byte cap before it is written.
            headers={"User-Agent": _user_agent(), "Accept-Encoding": "identity"},
            timeout=_TIMEOUT,
        ) as response:
            if response.status_code != 200:
                logger.warning("image %s returned %s", url, response.status_code)
                return False
            if _declared_length(response) > _MAX_BYTES:
                logger.warning("image %s declares too large a body", url)
                return False
            total = 0
            exceeded = False
            with tmp.open("wb") as handle:
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > _MAX_BYTES:
                        exceeded = True
                        break
                    handle.write(chunk)
            if exceeded:
                logger.warning("image %s exceeded the %d byte cap", url, _MAX_BYTES)
                _unlink_quietly(tmp)
                return False
    except httpx.HTTPError as error:
        logger.warning("image download failed for %s: %s", url, error)
        _unlink_quietly(tmp)
        return False

    if not _is_image(tmp):
        logger.warning("image %s did not decode as an image", url)
        _unlink_quietly(tmp)
        return False
    tmp.replace(dest)
    return True


def _declared_length(response: httpx.Response) -> int:
    """The Content-Length header as an int, or 0 when absent or malformed."""
    raw = response.headers.get("content-length")
    if raw is None or not raw.isdigit():
        return 0
    return int(raw)


def _is_image(path: Path) -> bool:
    """Whether the file at ``path`` decodes as an image."""
    try:
        with Image.open(path) as image:
            image.load()
        return True
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
        return False


def _write_caption(image_path: Path, title: str, artist: str) -> None:
    sidecar = image_path.with_name(image_path.name + ".txt")
    lines = [title] + ([artist] if artist else [])
    sidecar.write_text("\n".join(lines) + "\n")


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
