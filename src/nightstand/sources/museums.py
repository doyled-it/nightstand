"""Gather public-domain art from museum open-access APIs into the art share.

This is a hand-run helper, not part of the display loop: the owner points it at
the mounted art share and it downloads public-domain works with their captions.
It never writes into the repo, and it writes nothing identifying in a request.

Three sources are supported:

- ``met``: the Metropolitan Museum of Art Collection API. Search returns object
  ids; each object is fetched and kept only when ``isPublicDomain`` is true and
  it has a ``primaryImage``.
- ``aic``: the Art Institute of Chicago API. Search returns the fields inline;
  an item is kept only when ``is_public_domain`` is true and it has an
  ``image_id``, and its full-size image comes from the IIIF endpoint.
- ``commons``: Wikimedia Commons. The query is an artist name; paintings are
  found through the ``Paintings by <artist>`` category and its subcategories,
  and a file is kept only when its license metadata says public domain and its
  name does not mark it as a drawing, sketch, or photograph.

Every downloaded image is passed through a shape filter (too tall is skipped),
a faintness filter (tones too light to read on the six-ink panel are skipped),
and a colorfulness filter (works too drab to see are skipped), so a faint
graphite drawing or a black-and-white print never reaches the share.

Each kept item is written as ``<source>-<id>.jpg`` with a sidecar
``<source>-<id>.jpg.txt`` caption (line 1 title, line 2 artist, line 3 the https
URL of the museum object page), the same format the art library reads. The URL
line is optional; when it is present the artist line is always written (blank if
unknown) so the URL stays on the third line. An item whose image file already
exists is skipped, so a rerun adds only what is new.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx
from PIL import Image, UnidentifiedImageError

from nightstand.sources.coverart import _user_agent

logger = logging.getLogger(__name__)

_TIMEOUT = 30.0
_DEFAULT_LIMIT = 20
# Default minimum aspect (width/height). Works below this are portrait or near
# square and read poorly on the landscape panel, so they are not kept. This
# matches the hub's rotation cutoff (settings.min_aspect). The CLI can override
# it with --min-aspect.
_DEFAULT_MIN_ASPECT = 1.1
# A work whose 2nd-percentile luma is lighter than this reads as nearly blank on
# the six-ink panel (graphite on cream paper, faint washes), so it is skipped.
# Calibrated against the Met Klimt graphite "Two Women Reclining" (darkest tone
# 169 of 255): its 2nd percentile sits above the floor and it is skipped, while
# a painting with real shadows sits well below it and is kept.
_FAINT_FLOOR = 150
# A work whose Hasler-Suesstrunk colorfulness falls below this reads as drab on
# the six-ink panel: black-and-white etchings and lithographs score about 8 to
# 14 and the drabbest prints up to about 20, while clearly colorful paintings
# score 33 and up, so 25 cleanly separates them. --min-colorfulness overrides it
# and 0 disables the filter.
_DEFAULT_MIN_COLORFULNESS = 25
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

# Wikimedia Commons: the MediaWiki API finds paintings by a named artist through
# the "Paintings by <artist>" category (and its subcategories), then imageinfo
# gives each file's license metadata and a thumbnail URL. Wikimedia asks clients
# for a descriptive User-Agent naming the project and its repo (coverart's
# _user_agent already is that, with nothing personal), a modest request rate (we
# batch imageinfo lookups so the whole run is a handful of calls), and the
# thumbnail URL API rather than full originals.
_COMMONS_API = "https://commons.wikimedia.org/w/api.php"
_COMMONS_IMAGE_HOST = "upload.wikimedia.org"
_COMMONS_THUMB_WIDTH = 3000
_COMMONS_CATEGORY_LIMIT = 500
_COMMONS_IMAGEINFO_BATCH = 50
# Namespace numbers in a categorymembers response: 6 is File, 14 is Category.
_COMMONS_FILE_NS = 6
_COMMONS_SUBCAT_NS = 14
# Words in a filename or ObjectName that mark a work as not a painting. The
# "Paintings by <artist>" category is already paintings, and the faintness
# filter catches graphite that slips through; this drops the obvious non-paintings
# the metadata names outright.
_COMMONS_NON_PAINTING = ("drawing", "sketch", "photograph", "photo of", "frame")

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
    works were already on disk, ``skipped_shape`` how many downloaded images were
    discarded for being too tall, ``skipped_faint`` how many were discarded for
    being too faint to read on the panel, and ``skipped_drab`` how many were
    discarded for being too colorless. A kept work that is none of written,
    skipped, skipped_shape, skipped_faint, or skipped_drab failed to download;
    its HTTP status is logged.
    """

    found: int = 0
    kept: int = 0
    written: int = 0
    skipped: int = 0
    skipped_shape: int = 0
    skipped_faint: int = 0
    skipped_drab: int = 0


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
    min_colorfulness: float = _DEFAULT_MIN_COLORFULNESS,
) -> FetchResult:
    """Fetch up to ``limit`` public-domain works for ``query`` into ``dest``.

    A downloaded image narrower than ``min_aspect`` (width/height) is discarded
    and counted under ``skipped_shape``, one too faint to read under
    ``skipped_faint``, and one whose colorfulness is below ``min_colorfulness``
    under ``skipped_drab`` (a ``min_colorfulness`` of 0 disables that filter), so
    only landscape works that will actually read on the panel reach the share.
    """
    dest = Path(dest)
    if source == "met":
        headers = _met_headers()
        found, items = _met_items(client, query, limit, headers)
    elif source == "aic":
        headers = _aic_headers()
        found, items = _aic_items(client, query, limit, headers)
    elif source == "commons":
        headers = _commons_headers()
        found, items = _commons_items(client, query, limit, headers)
    else:
        raise ValueError(
            f"unknown source {source!r}; use 'met', 'aic', or 'commons'"
        )

    dest.mkdir(parents=True, exist_ok=True)
    written = 0
    skipped = 0
    skipped_shape = 0
    skipped_faint = 0
    skipped_drab = 0
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
        if _is_faint(image_path):
            _unlink_quietly(image_path)
            skipped_faint += 1
            continue
        if _is_drab(image_path, min_colorfulness):
            _unlink_quietly(image_path)
            skipped_drab += 1
            continue
        _write_caption(image_path, item.title, item.artist, item.source_url)
        written += 1
    return FetchResult(
        found=found,
        kept=len(items),
        written=written,
        skipped=skipped,
        skipped_shape=skipped_shape,
        skipped_faint=skipped_faint,
        skipped_drab=skipped_drab,
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


def _commons_headers() -> dict[str, str]:
    """Headers for Commons requests: our descriptive, repo-naming User-Agent.

    Wikimedia asks automated clients to identify themselves with a User-Agent
    that names the project and a contact or repo URL. coverart's _user_agent is
    exactly that, with nothing personal, so it is reused here.
    """
    return {"User-Agent": _user_agent()}


def _commons_items(
    client: httpx.Client, query: str, limit: int, headers: dict[str, str]
) -> tuple[int, list[_Item]]:
    """Find public-domain paintings by ``query`` (an artist name) on Commons."""
    titles = _commons_file_titles(client, query, limit, headers)
    items: list[_Item] = []
    for batch in _chunked(titles, _COMMONS_IMAGEINFO_BATCH):
        if len(items) >= limit:
            break
        info = _get_json(
            client,
            _COMMONS_API,
            {
                "action": "query",
                "format": "json",
                "prop": "imageinfo",
                "titles": "|".join(batch),
                "iiprop": "url|extmetadata|mime",
                "iiurlwidth": _COMMONS_THUMB_WIDTH,
            },
            headers,
        )
        pages = ((info or {}).get("query") or {}).get("pages") or {}
        for page in pages.values():
            if len(items) >= limit:
                break
            item = _commons_item(page, query)
            if item is not None:
                items.append(item)
    return len(titles), items


def _commons_file_titles(
    client: httpx.Client, artist: str, limit: int, headers: dict[str, str]
) -> list[str]:
    """File titles in ``Paintings by <artist>`` and its subcategories.

    One level of subcategories is followed, capped so a broad artist does not
    pull an unbounded number of candidates before the per-file filtering runs.
    """
    category = f"Category:Paintings by {artist}"
    files, subcats = _commons_category(client, category, headers)
    titles: list[str] = []
    seen: set[str] = set()
    for title in files:
        if title not in seen:
            seen.add(title)
            titles.append(title)
    for subcat in subcats:
        if len(titles) >= limit * 3:
            break
        subfiles, _ = _commons_category(client, subcat, headers)
        for title in subfiles:
            if title not in seen:
                seen.add(title)
                titles.append(title)
    return titles


def _commons_category(
    client: httpx.Client, category: str, headers: dict[str, str]
) -> tuple[list[str], list[str]]:
    """The file titles and subcategory titles directly in ``category``."""
    data = _get_json(
        client,
        _COMMONS_API,
        {
            "action": "query",
            "format": "json",
            "list": "categorymembers",
            "cmtitle": category,
            "cmtype": "file|subcat",
            "cmlimit": _COMMONS_CATEGORY_LIMIT,
        },
        headers,
    )
    members = ((data or {}).get("query") or {}).get("categorymembers") or []
    files = [
        str(m["title"])
        for m in members
        if m.get("ns") == _COMMONS_FILE_NS and m.get("title")
    ]
    subcats = [
        str(m["title"])
        for m in members
        if m.get("ns") == _COMMONS_SUBCAT_NS and m.get("title")
    ]
    return files, subcats


def _commons_item(page: dict, artist: str) -> _Item | None:
    """Build an ``_Item`` from a Commons page, or ``None`` if it is not kept."""
    infos = page.get("imageinfo") or []
    if not infos:
        return None
    info = infos[0]
    extmeta = info.get("extmetadata") or {}
    title = str(page.get("title") or "")
    if not str(info.get("mime") or "").startswith("image/"):
        return None
    if not _commons_is_public_domain(extmeta):
        return None
    if not _commons_looks_like_painting(title, extmeta):
        return None
    thumb = info.get("thumburl")
    if not isinstance(thumb, str) or not _valid_commons_image(thumb):
        logger.warning(
            "skipping Commons file %s: thumbnail URL not on the trusted host",
            title,
        )
        return None
    try:
        pageid = int(page.get("pageid"))
    except (TypeError, ValueError):
        return None
    return _Item(
        object_id=pageid,
        image_url=thumb,
        title=_commons_title(title, extmeta),
        artist=artist,
        source_url=_commons_file_page(title),
    )


def _commons_meta(extmeta: dict, key: str) -> str:
    """The string value of an extmetadata field, or an empty string."""
    field = extmeta.get(key)
    if isinstance(field, dict) and field.get("value") is not None:
        return str(field["value"]).strip()
    return ""


def _commons_is_public_domain(extmeta: dict) -> bool:
    """Whether the license metadata marks the file as public domain.

    A positive public-domain signal is required, so a freely licensed but still
    copyrighted work (CC BY, say) is not kept.
    """
    license_short = _commons_meta(extmeta, "LicenseShortName").lower()
    license_id = _commons_meta(extmeta, "License").lower()
    copyrighted = _commons_meta(extmeta, "Copyrighted").lower()
    if "public domain" in license_short:
        return True
    if license_id in {"pd", "cc0", "cc-zero"} or license_id.startswith("pd-"):
        return True
    return copyrighted == "false"


def _commons_looks_like_painting(title: str, extmeta: dict) -> bool:
    """Whether nothing in the name or metadata marks the file as not a painting."""
    haystack = f"{title} {_commons_meta(extmeta, 'ObjectName')}".lower()
    return not any(word in haystack for word in _COMMONS_NON_PAINTING)


def _commons_title(title: str, extmeta: dict) -> str:
    """A caption title: the ObjectName if present, else the bare filename."""
    name = _commons_meta(extmeta, "ObjectName")
    if name:
        return name
    stem = title[len("File:") :] if title.startswith("File:") else title
    return stem.rsplit(".", 1)[0]


def _commons_file_page(title: str) -> str:
    """The https Commons file page URL for ``title``."""
    return "https://commons.wikimedia.org/wiki/" + quote(
        title.replace(" ", "_"), safe=":/"
    )


def _valid_commons_image(url: str) -> bool:
    """Whether ``url`` is an https URL on the Commons upload host."""
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.hostname == _COMMONS_IMAGE_HOST


def _chunked(items: list[str], size: int) -> list[list[str]]:
    """Split ``items`` into lists of at most ``size`` (empty in, empty out)."""
    return [items[i : i + size] for i in range(0, len(items), size)]


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


def _is_faint(path: Path) -> bool:
    """Whether the image is too faint to read on the six-ink panel.

    The image is reduced to a small grayscale thumbnail and its 2nd-percentile
    luma is measured: when even the darkest few percent of the work is lighter
    than ``_FAINT_FLOOR``, nothing on it would register as ink on the panel. A
    file that cannot be read is not called faint (it already decoded once during
    download), so it passes through to the normal failure path.
    """
    floor = _tonal_floor(path)
    return floor is not None and floor > _FAINT_FLOOR


def _tonal_floor(path: Path) -> int | None:
    """The 2nd-percentile luma of a grayscale downscale, or ``None`` on failure."""
    try:
        with Image.open(path) as image:
            gray = image.convert("L")
            gray.thumbnail((64, 64))
            histogram = gray.histogram()
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
        return None
    total = sum(histogram)
    if total == 0:
        return None
    target = int(total * 0.02)
    cumulative = 0
    for value, count in enumerate(histogram):
        cumulative += count
        if cumulative > target:
            return value
    return 255


def _is_drab(path: Path, threshold: float) -> bool:
    """Whether the image is too colorless to see on the six-ink panel.

    A ``threshold`` of 0 (or less) disables the check. An image that cannot be
    read is not called drab (it already decoded once during download), so it
    passes through to the normal failure path.
    """
    if threshold <= 0:
        return False
    score = _colorfulness(path)
    return score is not None and score < threshold


def _colorfulness(path: Path) -> float | None:
    """The Hasler-Suesstrunk colorfulness of a small downscale, or ``None``.

    ``M = sqrt(sd_rg^2 + sd_yb^2) + 0.3 * sqrt(mean_rg^2 + mean_yb^2)`` with
    ``rg = R - G`` and ``yb = 0.5(R + G) - B`` (Hasler and Suesstrunk, "Measuring
    colourfulness in natural images", 2003). A near-grayscale print scores in the
    single digits, a vivid painting in the tens.
    """
    try:
        with Image.open(path) as image:
            rgb = image.convert("RGB")
            rgb.thumbnail((64, 64))
            data = rgb.tobytes()
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
        return None
    count = len(data) // 3
    if count == 0:
        return None
    sum_rg = sum_rg2 = sum_yb = sum_yb2 = 0.0
    for i in range(0, count * 3, 3):
        red, green, blue = data[i], data[i + 1], data[i + 2]
        rg = red - green
        yb = 0.5 * (red + green) - blue
        sum_rg += rg
        sum_rg2 += rg * rg
        sum_yb += yb
        sum_yb2 += yb * yb
    mean_rg = sum_rg / count
    mean_yb = sum_yb / count
    var_rg = max(sum_rg2 / count - mean_rg * mean_rg, 0.0)
    var_yb = max(sum_yb2 / count - mean_yb * mean_yb, 0.0)
    std_root = math.sqrt(var_rg + var_yb)
    mean_root = math.sqrt(mean_rg * mean_rg + mean_yb * mean_yb)
    return std_root + 0.3 * mean_root


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
