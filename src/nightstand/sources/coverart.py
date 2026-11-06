"""Cover art for the now-playing screen, fetched once and cached on the device.

A track either carries its own art URL (AirPlay sends one) or it does not
(Bluetooth sends none). When it does, the image is fetched directly. When it
does not but the track names an album, MusicBrainz is searched for the release
and the Cover Art Archive serves the front cover.

Everything is cached under the cache directory so a track is fetched at most
once: a successful image under ``sha1`` of its key, and a MusicBrainz miss as a
small ``.none`` marker that suppresses the lookup for seven days. MusicBrainz
asks callers for no more than one request per second and a descriptive
User-Agent, so both are honoured here. The User-Agent identifies the project by
its public repository URL and carries nothing personal.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import io
import logging
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

import httpx
from PIL import Image, UnidentifiedImageError

from nightstand.models import NowPlaying

logger = logging.getLogger(__name__)

# MusicBrainz asks callers to identify themselves. The version comes from the
# installed package metadata so it tracks the release, and the contact is the
# public repo URL, with nothing personal.
USER_AGENT = (
    f"nightstand/{importlib.metadata.version('nightstand')} "
    "( https://github.com/doyled-it/nightstand )"
)

_MUSICBRAINZ_SEARCH = "https://musicbrainz.org/ws/2/release/"
_COVER_ART_ARCHIVE = "https://coverartarchive.org/release/{mbid}/front-500"
_MUSICBRAINZ_MIN_INTERVAL = 1.0  # seconds between MusicBrainz requests
_MISS_TTL = 7 * 24 * 60 * 60  # a cached miss is honoured for seven days
_NONE_SUFFIX = ".none"
_TIMEOUT = 10.0
# A cover is tiny; cap the download so a hostile or broken source cannot stream
# an unbounded body into memory on the device. Compression is refused on the
# request, so the byte cap bounds the raw body, and the pixel cap rejects an
# outsized image by its declared dimensions before it is decoded.
_MAX_BYTES = 10 * 1024 * 1024
_MAX_PIXELS = 4096 * 4096


class _TransientLookupError(Exception):
    """A lookup request failed in a way that should be retried later.

    Raised for transport errors, timeouts, 5xx responses, and unparseable
    bodies. The caller returns ``None`` without caching a miss, so a passing
    network blip does not suppress an album's art for the whole miss window.
    """


class CoverArt:
    """Resolve and cache the cover image for a now-playing track."""

    def __init__(
        self,
        client: httpx.Client,
        cache_dir: Path,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self._cache_dir = Path(cache_dir)
        self._clock = clock
        self._sleep = sleep
        self._last_musicbrainz: float | None = None

    def for_track(self, np: NowPlaying) -> Image.Image | None:
        """The cover for ``np``, from the cache or the network, or ``None``."""
        if np.art_url:
            return self._from_url(np.art_url)
        if np.album:
            return self._from_musicbrainz(np.artist, np.album)
        return None

    def _from_url(self, url: str) -> Image.Image | None:
        if urlparse(url).scheme.lower() not in ("http", "https"):
            # art_url comes from the playback sender, so only fetch plain web
            # URLs. This refuses file://, data:// and other schemes outright.
            logger.warning("refusing non-http art URL")
            return None
        path = self._cache_path(_sha1(url))
        cached = self._load_cached(path)
        if cached is not None:
            return cached
        try:
            data = self._get(url)
        except _TransientLookupError as error:
            logger.warning("art URL fetch failed: %s", error)
            return None
        image = _decode(data)
        if image is None:
            return None
        self._write(path, data)
        return image

    def _from_musicbrainz(self, artist: str, album: str) -> Image.Image | None:
        path = self._cache_path(_sha1(f"{artist}|{album}"))
        cached = self._load_cached(path)
        if cached is not None:
            return cached
        marker = path.with_name(path.name + _NONE_SUFFIX)
        if self._marker_fresh(marker):
            return None

        try:
            mbid = self._search_release(artist, album)
            data = None
            if mbid is not None:
                url = _COVER_ART_ARCHIVE.format(mbid=mbid)
                data = self._get(url, follow_redirects=True)
        except _TransientLookupError as error:
            # The lookup itself failed; retry next time rather than cache a miss.
            logger.warning("cover lookup failed: %s", error)
            return None

        image = _decode(data) if data is not None else None
        if image is None:
            # A genuine miss: MusicBrainz has no such release, or the archive has
            # no usable cover for it. Suppress the lookup for the miss window.
            self._write(marker, b"")
            return None
        self._write(path, data)
        return image

    def _search_release(self, artist: str, album: str) -> str | None:
        """The MusicBrainz release id, or ``None`` when no release was found.

        Raises ``_TransientLookupError`` when the request itself failed, so a
        failure is never mistaken for a genuine "no release" result.
        """
        self._await_musicbrainz_gate()
        try:
            response = self._client.get(
                _MUSICBRAINZ_SEARCH,
                params={
                    "query": f'artist:"{artist}" AND release:"{album}"',
                    "fmt": "json",
                    "limit": 1,
                },
                headers={"User-Agent": USER_AGENT},
                timeout=_TIMEOUT,
            )
        except httpx.HTTPError as error:
            raise _TransientLookupError(
                f"MusicBrainz request failed: {error}"
            ) from error
        finally:
            self._last_musicbrainz = self._clock()
        if response.status_code != 200:
            raise _TransientLookupError(
                f"MusicBrainz returned {response.status_code}"
            )
        try:
            releases = response.json().get("releases", [])
        except (ValueError, AttributeError) as error:
            raise _TransientLookupError(
                f"MusicBrainz body unparseable: {error}"
            ) from error
        if not isinstance(releases, list) or not releases:
            return None
        first = releases[0]
        return first.get("id") if isinstance(first, dict) else None

    def _await_musicbrainz_gate(self) -> None:
        """Wait, if needed, so MusicBrainz is called at most once per second."""
        if self._last_musicbrainz is None:
            return
        elapsed = self._clock() - self._last_musicbrainz
        remaining = _MUSICBRAINZ_MIN_INTERVAL - elapsed
        if remaining > 0:
            self._sleep(remaining)

    def _get(self, url: str, *, follow_redirects: bool = False) -> bytes | None:
        """Download ``url``, capping the body at ``_MAX_BYTES``.

        Returns the body on a 200, ``None`` on a 404 (the resource is
        definitively absent), and raises ``_TransientLookupError`` on any other
        failure so the caller can retry rather than cache a miss.
        """
        try:
            with self._client.stream(
                "GET",
                url,
                # Refuse content-encoding so a small compressed body cannot
                # amplify past the byte cap; covers are already-compressed images.
                headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
                follow_redirects=follow_redirects,
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

    def _marker_fresh(self, marker: Path) -> bool:
        try:
            age = time.time() - marker.stat().st_mtime
        except OSError:
            return False
        return age < _MISS_TTL

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
