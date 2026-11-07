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
``<source>-<id>.jpg.txt`` caption (line 1 title, line 2 artist, line 3 the https
URL of the museum object page), the same format the art library reads. The URL
line is optional; when it is present the artist line is always written (blank if
unknown) so the URL stays on the third line. An item whose image file already
exists is skipped, so a rerun adds only what is new.
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
# Default minimum aspect (width/height). Works below this are portrait or near
# square and read poorly on the landscape panel, so they are not kept. The CLI
# can override it with --min-aspect.
_DEFAULT_MIN_ASPECT = 1.3
# Cap each image so a hostile or broken response cannot stream an unbounded body
# onto the device. Museum originals are large but comfortably under this.
_MAX_BYTES = 64 * 1024 * 1024

# The Met retired the v1 search on 2026-10-01 (it now answers 410); search moved
# to the Elastic-based v1.1 endpoint. There is no v1.1 object endpoint (it
# answers 404), so object lookups stay on v1.
_MET_SEARCH = "https://collectionapi.metmuseum.org/public/collection/v1.1/search"
_MET_OBJECT = "https://collectionapi.metmuseum.org/public/collection/v1/objects/{id}"
_AIC_SEARCH = "https://api.artic.edu/api/v1/artworks/search"
_AIC_IMAGE = "https://www.artic.edu/iiif/2/{image_id}/full/843,/0/default.jpg"

# The Art Institute asks clients to identify themselves with an AIC-User-Agent
# header carrying the app name and a contact URL (no email, per the privacy
# rule). Its image CDN (Cloudflare) answers 403 to our "nightstand/<version>
# (<repo>)" User-Agent, so AIC requests leave the User-Agent to httpx's library
# default, which the CDN accepts. This was verified live against both the search
# API and the IIIF image server. We do not spoof a browser string.
_AIC_IDENTITY = "nightstand (https://github.com/doyled-it/nightstand)"

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
    """What one query found and did, so a "0 written" run is never silent.

    ``found`` is the number of search hits the query matched, ``kept`` how many
    were kept as public-domain works with a usable image (capped at the limit),
    ``written`` how many images this run downloaded, ``skipped`` how many kept
    works were already on disk, and ``skipped_shape`` how many downloaded images
    were discarded for being too tall. A kept work that is none of written,
    skipped, or skipped_shape failed to download; its HTTP status is logged.
    """

    found: int = 0
    kept: int = 0
    written: int = 0
    skipped: int = 0
    skipped_shape: int = 0


@dataclass(frozen=True)
class _Item:
    """One kept work: its output id, image URL, caption, and source page."""

    object_id: int
    image_url: str
    title: str
    artist: str
    source_url: str = ""


def fetch_museum(
    client: httpx.Client,
    source: str,
    query: str,
    dest: Path,
    limit: int = _DEFAULT_LIMIT,
    min_aspect: float = _DEFAULT_MIN_ASPECT,
) -> FetchResult:
    """Fetch up to ``limit`` public-domain works for ``query`` into ``dest``.

    A downloaded image narrower than ``min_aspect`` (width/height) is discarded
    and counted under ``skipped_shape``, so only landscape works reach the share.
    """
    dest = Path(dest)
    if source == "met":
        headers = _met_headers()
        found, items = _met_items(client, query, limit, headers)
    elif source == "aic":
        headers = _aic_headers()
        found, items = _aic_items(client, query, limit, headers)
    else:
        raise ValueError(f"unknown museum source {source!r}; use 'met' or 'aic'")

    dest.mkdir(parents=True, exist_ok=True)
    written = 0
    skipped = 0
    skipped_shape = 0
    for item in items:
        image_path = dest / f"{source}-{item.object_id}.jpg"
        if image_path.exists():
            skipped += 1
            continue
        if not _download(client, item.image_url, image_path, headers):
            continue
        aspect = _image_aspect(image_path)
        if aspect is None or aspect < min_aspect:
            _unlink_quietly(image_path)
            skipped_shape += 1
            continue
        _write_caption(image_path, item.title, item.artist, item.source_url)
        written += 1
    return FetchResult(
        found=found,
        kept=len(items),
        written=written,
        skipped=skipped,
        skipped_shape=skipped_shape,
    )


def _met_headers() -> dict[str, str]:
    """Headers for Met requests: our identifying User-Agent, which it accepts."""
    return {"User-Agent": _user_agent()}


def _aic_headers() -> dict[str, str]:
    """Headers for AIC requests: the AIC-User-Agent identity, library-default UA.

    The User-Agent is left unset so httpx sends its default; AIC's image CDN
    rejects our "nightstand/<version> (<repo>)" User-Agent with 403.
    """
    return {"AIC-User-Agent": _AIC_IDENTITY}


def _met_items(
    client: httpx.Client, query: str, limit: int, headers: dict[str, str]
) -> tuple[int, list[_Item]]:
    search = _get_json(
        client, _MET_SEARCH, {"hasImages": "true", "q": query}, headers
    )
    object_ids = (search or {}).get("objectIDs") or []
    items: list[_Item] = []
    for object_id in object_ids:
        if len(items) >= limit:
            break
        obj = _get_json(client, _MET_OBJECT.format(id=object_id), None, headers)
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
                source_url=_https_or_empty(obj.get("objectURL")),
            )
        )
    return len(object_ids), items


def _aic_items(
    client: httpx.Client, query: str, limit: int, headers: dict[str, str]
) -> tuple[int, list[_Item]]:
    search = _get_json(
        client,
        _AIC_SEARCH,
        {
            "q": query,
            "fields": "id,title,artist_title,image_id,is_public_domain",
            "limit": limit,
        },
        headers,
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
        item_id = int(entry["id"])
        items.append(
            _Item(
                object_id=item_id,
                image_url=_AIC_IMAGE.format(image_id=image_id),
                title=str(entry.get("title") or ""),
                artist=str(entry.get("artist_title") or ""),
                source_url=f"https://www.artic.edu/artworks/{item_id}",
            )
        )
    return len(data), items


def _get_json(
    client: httpx.Client, url: str, params: dict | None, headers: dict[str, str]
) -> dict | None:
    """GET ``url`` and return the parsed JSON object, or ``None`` on any failure."""
    try:
        response = client.get(
            url,
            params=params,
            headers=headers,
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


def _https_or_empty(value: object) -> str:
    """``value`` when it is an https URL string, else an empty string.

    The source page URL comes from the API response, so a non-https or
    non-string value is dropped rather than written into the caption.
    """
    if isinstance(value, str) and urlparse(value).scheme == "https":
        return value
    return ""


def _download(
    client: httpx.Client, url: str, dest: Path, headers: dict[str, str]
) -> bool:
    """Stream ``url`` to a temp file and rename it to ``dest``; skip on failure.

    The body is capped so a hostile or broken source cannot fill the disk, and it
    is confirmed to decode as an image before it is renamed into place, so only a
    usable image ever lands in the destination.
    """
    tmp = dest.with_name(dest.name + ".part")
    # Refuse content-encoding so a small compressed body cannot amplify past the
    # byte cap before it is written.
    request_headers = {**headers, "Accept-Encoding": "identity"}
    try:
        with client.stream(
            "GET",
            url,
            headers=request_headers,
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


def _image_aspect(path: Path) -> float | None:
    """The image's width/height, or ``None`` if it cannot be read or is zero."""
    try:
        with Image.open(path) as image:
            width, height = image.size
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
        return None
    if height == 0:
        return None
    return width / height


def _write_caption(
    image_path: Path, title: str, artist: str, source_url: str
) -> None:
    """Write the sidecar: line 1 title, line 2 artist, line 3 source page URL.

    When a source URL is present the artist line is always emitted (blank if
    unknown) so the URL stays on the third line. With no URL the caption is the
    title and, when known, the artist.
    """
    sidecar = image_path.with_name(image_path.name + ".txt")
    if source_url:
        lines = [title, artist, source_url]
    else:
        lines = [title] + ([artist] if artist else [])
    sidecar.write_text("\n".join(lines) + "\n")


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
