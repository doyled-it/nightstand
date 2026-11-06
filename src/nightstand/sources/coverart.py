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

USER_AGENT = "nightstand/0.1 (https://github.com/doyled-it/nightstand)"

_MUSICBRAINZ_SEARCH = "https://musicbrainz.org/ws/2/release/"
_COVER_ART_ARCHIVE = "https://coverartarchive.org/release/{mbid}/front-500"
_MUSICBRAINZ_MIN_INTERVAL = 1.0  # seconds between MusicBrainz requests
_MISS_TTL = 7 * 24 * 60 * 60  # a cached miss is honoured for seven days
_NONE_SUFFIX = ".none"
_TIMEOUT = 10.0
# A cover is tiny; cap the download so a hostile or broken source cannot stream
# an unbounded body into memory on the device.
_MAX_BYTES = 10 * 1024 * 1024


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
        data = self._get(url)
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

        mbid = self._search_release(artist, album)
        data = None
        if mbid is not None:
            url = _COVER_ART_ARCHIVE.format(mbid=mbid)
            data = self._get(url, follow_redirects=True)
        image = _decode(data)
        if image is None:
            self._write(marker, b"")
            return None
        self._write(path, data)
        return image

    def _search_release(self, artist: str, album: str) -> str | None:
        """The MusicBrainz release id for ``artist``/``album``, or ``None``."""
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
            logger.warning("MusicBrainz search failed: %s", error)
            return None
        finally:
            self._last_musicbrainz = self._clock()
        if response.status_code != 200:
            return None
        releases = response.json().get("releases", [])
        return releases[0].get("id") if releases else None

    def _await_musicbrainz_gate(self) -> None:
        """Wait, if needed, so MusicBrainz is called at most once per second."""
        if self._last_musicbrainz is None:
            return
        elapsed = self._clock() - self._last_musicbrainz
        remaining = _MUSICBRAINZ_MIN_INTERVAL - elapsed
        if remaining > 0:
            self._sleep(remaining)

    def _get(self, url: str, *, follow_redirects: bool = False) -> bytes | None:
        """Download ``url``, stopping if the body exceeds ``_MAX_BYTES``."""
        try:
            with self._client.stream(
                "GET",
                url,
                headers={"User-Agent": USER_AGENT},
                follow_redirects=follow_redirects,
                timeout=_TIMEOUT,
            ) as response:
                if response.status_code != 200:
                    return None
                if _declared_length(response) > _MAX_BYTES:
                    logger.warning("cover at %s declares too large a body", url)
                    return None
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > _MAX_BYTES:
                        logger.warning("cover at %s exceeded the size cap", url)
                        return None
                    chunks.append(chunk)
                return b"".join(chunks)
        except httpx.HTTPError as error:
            logger.warning("cover fetch failed for %s: %s", url, error)
            return None

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

    The bytes are untrusted, so any failure, including a Pillow decompression
    bomb guard firing, resolves to ``None`` rather than raising into the caller.
    """
    if not data:
        return None
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            return image.copy()
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
        return None
