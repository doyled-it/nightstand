"""Cover art for the now-playing screen, fetched once and cached on the device.

The hub resolves artwork for every track (including the Bluetooth tracks that
arrive with none) and publishes a same-origin ``art_url`` path such as
``/cover/<sha>.jpg``. The frame does no artwork lookup of its own: it builds the
absolute URL from the configured hub host, fetches the image, and caches it
under the cache directory keyed by a hash of that URL, so a track's cover is
fetched at most once. A track with no ``art_url`` has no cover.

The download is bounded: the body is capped, compression is refused so a small
compressed body cannot amplify past the cap, and an image whose declared
dimensions exceed the cover-sized pixel cap is rejected before it is decoded.
The bytes are untrusted, so any decode failure resolves to ``None``. The request
carries a User-Agent that identifies the project by its public repository URL
and nothing personal.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import io
import logging
from pathlib import Path
from urllib.parse import urlparse

import httpx
from PIL import Image, UnidentifiedImageError

from nightstand.models import NowPlaying

logger = logging.getLogger(__name__)

# The request identifies itself with the project and its public repo URL, with
# nothing personal. Resolved lazily on first use and cached, with an "unknown"
# fallback, so importing nightstand from a source checkout that was never
# installed cannot raise at import time.
_user_agent_cache: str | None = None


def _package_version() -> str:
    try:
        return importlib.metadata.version("nightstand")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _user_agent() -> str:
    global _user_agent_cache
    if _user_agent_cache is None:
        _user_agent_cache = (
            f"nightstand/{_package_version()} "
            "( https://github.com/doyled-it/nightstand )"
        )
    return _user_agent_cache


def __getattr__(name: str) -> str:
    # Keep ``coverart.USER_AGENT`` working as a lazily-resolved module attribute.
    if name == "USER_AGENT":
        return _user_agent()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


_TIMEOUT = 10.0
# A cover is tiny; cap the download so a hostile or broken source cannot stream
# an unbounded body into memory on the device. Compression is refused on the
# request, so the byte cap bounds the raw body, and the pixel cap rejects an
# outsized image by its declared dimensions before it is decoded.
_MAX_BYTES = 10 * 1024 * 1024
_MAX_PIXELS = 4096 * 4096


class _TransientLookupError(Exception):
    """A fetch failed in a way that should be retried later.

    Raised for transport errors, timeouts, and non-200/404 responses. The caller
    returns ``None`` without caching anything, so a passing network blip does not
    suppress a cover permanently.
    """


class CoverArt:
    """Resolve and cache the cover image for a now-playing track."""

    def __init__(
        self,
        client: httpx.Client,
        cache_dir: Path,
        hub_base: str,
    ) -> None:
        self._client = client
        self._cache_dir = Path(cache_dir)
        # The hub's HTTP origin, e.g. ``http://house.local``, that a same-origin
        # ``art_url`` path is resolved against.
        self._hub_base = hub_base.rstrip("/")

    def for_track(self, np: NowPlaying) -> Image.Image | None:
        """The cover for ``np``, from the cache or the hub, or ``None``."""
        if not np.art_url:
            return None
        url = self._absolute(np.art_url)
        if url is None:
            return None
        return self._from_url(url)

    def _absolute(self, art_url: str) -> str | None:
        """The absolute hub URL for a same-origin ``art_url`` path.

        The contract makes ``art_url`` a same-origin path; it is resolved against
        the configured hub origin. A value that already carries a scheme is
        unexpected, so only plain http(s) is allowed through and anything else
        (``file:``, ``data:``) is refused.
        """
        scheme = urlparse(art_url).scheme.lower()
        if scheme in ("http", "https"):
            return art_url
        if scheme:
            logger.warning("refusing non-http art URL")
            return None
        return f"{self._hub_base}/{art_url.lstrip('/')}"

    def _from_url(self, url: str) -> Image.Image | None:
        path = self._cache_path(_sha1(url))
        cached = self._load_cached(path)
        if cached is not None:
            return cached
        try:
            data = self._get(url)
        except _TransientLookupError as error:
            logger.warning("cover fetch failed: %s", error)
            return None
        image = _decode(data)
        if image is None:
            return None
        self._write(path, data)
        return image

    def _get(self, url: str) -> bytes | None:
        """Download ``url``, capping the body at ``_MAX_BYTES``.

        Returns the body on a 200, ``None`` on a 404 (the cover is definitively
        absent), and raises ``_TransientLookupError`` on any other failure.
        """
        try:
            with self._client.stream(
                "GET",
                url,
                # Refuse content-encoding so a small compressed body cannot
                # amplify past the byte cap; covers are already-compressed images.
                headers={"User-Agent": _user_agent(), "Accept-Encoding": "identity"},
                timeout=_TIMEOUT,
            ) as response:
                if response.status_code == 404:
                    return None
                if response.status_code != 200:
                    raise _TransientLookupError(
                        f"{url} returned {response.status_code}"
                    )
                if _declared_length(response) > _MAX_BYTES:
                    raise _TransientLookupError(f"{url} declares too large a body")
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > _MAX_BYTES:
                        raise _TransientLookupError(f"{url} exceeded the size cap")
                    chunks.append(chunk)
                return b"".join(chunks)
        except httpx.HTTPError as error:
            raise _TransientLookupError(f"fetch failed for {url}: {error}") from error

    def _cache_path(self, digest: str) -> Path:
        return self._cache_dir / digest

    def _load_cached(self, path: Path) -> Image.Image | None:
        if not path.is_file():
            return None
        return _decode(path.read_bytes())

    def _write(self, path: Path, data: bytes) -> None:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def _sha1(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def _declared_length(response: httpx.Response) -> int:
    """The Content-Length header as an int, or 0 when absent or malformed."""
    raw = response.headers.get("content-length")
    if raw is None or not raw.isdigit():
        return 0
    return int(raw)


def _decode(data: bytes | None) -> Image.Image | None:
    """Load ``data`` as an image, or ``None`` if it is empty or not an image.

    The bytes are untrusted, so any failure resolves to ``None`` rather than
    raising into the caller. An image whose declared dimensions exceed the
    cover-sized pixel cap is rejected before it is decoded, which stops an
    outsized image short of Pillow's own 2x decompression-bomb threshold.
    """
    if not data:
        return None
    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            if width * height > _MAX_PIXELS:
                logger.warning("cover is %dx%d, past the pixel cap", width, height)
                return None
            image.load()
            return image.copy()
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
        return None
