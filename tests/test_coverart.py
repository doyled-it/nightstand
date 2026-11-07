from __future__ import annotations

import importlib.metadata
import io
import os
import time

import httpx
from PIL import Image

from nightstand.models import NowPlaying
from nightstand.sources import coverart
from nightstand.sources.coverart import USER_AGENT, CoverArt

MBID = "11111111-2222-3333-4444-555555555555"


def _png_bytes(color: tuple[int, int, int] = (10, 20, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buf, format="PNG")
    return buf.getvalue()


class FakeServer:
    """A MockTransport handler that records calls and serves canned responses."""

    def __init__(
        self,
        *,
        releases: list[dict] | None = None,
        cover_bytes: bytes | None = None,
        art_bytes: bytes | None = None,
    ) -> None:
        self.releases = [{"id": MBID}] if releases is None else releases
        self.cover_bytes = cover_bytes
        self.art_bytes = art_bytes
        self.calls: list[tuple[str, str]] = []
        self.user_agents: list[str | None] = []
        self.accept_encodings: list[str | None] = []

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def count(self, host: str) -> int:
        return sum(1 for call_host, _ in self.calls if call_host == host)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        self.calls.append((host, request.url.path))
        self.user_agents.append(request.headers.get("user-agent"))
        self.accept_encodings.append(request.headers.get("accept-encoding"))
        if host == "musicbrainz.org":
            return httpx.Response(200, json={"releases": self.releases})
        if host == "coverartarchive.org":
            if self.cover_bytes is None:
                return httpx.Response(404)
            return httpx.Response(
                200, content=self.cover_bytes, headers={"content-type": "image/png"}
            )
        # Any other host is the sender-supplied art_url.
        if self.art_bytes is None:
            return httpx.Response(404)
        return httpx.Response(200, content=self.art_bytes)


class FakeClock:
    """A monotonic clock whose sleep advances the clock instead of blocking."""

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


def _bluetooth(album: str | None = "Blue Hour") -> NowPlaying:
    return NowPlaying(title="Tidal Drift", artist="Nova Reef", album=album)


def _airplay() -> NowPlaying:
    return NowPlaying(
        title="Tidal Drift",
        artist="Nova Reef",
        album="Blue Hour",
        art_url="http://hub.local/art/blue-hour.jpg",
    )


def test_art_url_hit_fetches_once_then_reads_cache(tmp_path):
    server = FakeServer(art_bytes=_png_bytes())
    with httpx.Client(transport=server.transport) as client:
        art = CoverArt(client, tmp_path)
        first = art.for_track(_airplay())
        second = art.for_track(_airplay())

    assert isinstance(first, Image.Image)
    assert isinstance(second, Image.Image)
    assert server.count("hub.local") == 1


def test_art_url_skips_musicbrainz(tmp_path):
    server = FakeServer(art_bytes=_png_bytes())
    with httpx.Client(transport=server.transport) as client:
        CoverArt(client, tmp_path).for_track(_airplay())

    assert server.count("musicbrainz.org") == 0
    assert server.count("coverartarchive.org") == 0


def test_musicbrainz_hit_fetches_then_reads_cache(tmp_path):
    clock = FakeClock()
    server = FakeServer(cover_bytes=_png_bytes())
    with httpx.Client(transport=server.transport) as client:
        art = CoverArt(client, tmp_path, clock=clock.now, sleep=clock.sleep)
        first = art.for_track(_bluetooth())
        before = len(server.calls)
        second = art.for_track(_bluetooth())

    assert isinstance(first, Image.Image)
    assert isinstance(second, Image.Image)
    assert server.count("musicbrainz.org") == 1
    assert server.count("coverartarchive.org") == 1
    # The second lookup made no further requests.
    assert len(server.calls) == before


def test_musicbrainz_miss_writes_none_marker_and_does_not_refetch(tmp_path):
    clock = FakeClock()
    server = FakeServer(releases=[])
    with httpx.Client(transport=server.transport) as client:
        art = CoverArt(client, tmp_path, clock=clock.now, sleep=clock.sleep)
        first = art.for_track(_bluetooth())
        after_first = len(server.calls)
        second = art.for_track(_bluetooth())

    assert first is None
    assert second is None
    assert server.count("musicbrainz.org") == 1
    assert len(server.calls) == after_first
    # A .none marker was written next to where the image would have cached.
    markers = list(tmp_path.glob("*.none"))
    assert len(markers) == 1


def test_musicbrainz_miss_refetches_after_marker_expires(tmp_path):
    clock = FakeClock()
    server = FakeServer(releases=[])
    with httpx.Client(transport=server.transport) as client:
        art = CoverArt(client, tmp_path, clock=clock.now, sleep=clock.sleep)
        art.for_track(_bluetooth())
        marker = next(iter(tmp_path.glob("*.none")))
        eight_days_ago = time.time() - 8 * 24 * 60 * 60
        os.utime(marker, (eight_days_ago, eight_days_ago))
        art.for_track(_bluetooth())

    assert server.count("musicbrainz.org") == 2


def test_user_agent_is_exact_and_has_no_at_sign(tmp_path):
    server = FakeServer(cover_bytes=_png_bytes())
    with httpx.Client(transport=server.transport) as client:
        CoverArt(client, tmp_path).for_track(_bluetooth())

    assert server.user_agents, "expected at least one outbound request"
    assert all(ua == USER_AGENT for ua in server.user_agents)
    assert "@" not in USER_AGENT


def test_user_agent_matches_the_package_version(tmp_path):
    version = importlib.metadata.version("nightstand")
    assert USER_AGENT == (
        f"nightstand/{version} ( https://github.com/doyled-it/nightstand )"
    )


def test_user_agent_falls_back_to_unknown_without_package_metadata(monkeypatch):
    def raise_missing(name):
        raise importlib.metadata.PackageNotFoundError(name)

    # Reset the lazy cache so the fallback path runs, then make metadata missing.
    monkeypatch.setattr(coverart, "_user_agent_cache", None)
    monkeypatch.setattr(importlib.metadata, "version", raise_missing)

    ua = coverart._user_agent()

    assert ua == "nightstand/unknown ( https://github.com/doyled-it/nightstand )"


def test_malformed_200_body_returns_none_and_caches_no_miss(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "musicbrainz.org"
        return httpx.Response(200, content=b"<html>not json</html>")

    clock = FakeClock()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        art = CoverArt(client, tmp_path, clock=clock.now, sleep=clock.sleep)
        result = art.for_track(_bluetooth())

    assert result is None
    # A broken body is a failure, not a genuine miss, so nothing is cached.
    assert list(tmp_path.glob("*.none")) == []


def test_valid_json_missing_release_keys_returns_none(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "musicbrainz.org"
        return httpx.Response(200, json={"unexpected": "shape"})

    clock = FakeClock()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        art = CoverArt(client, tmp_path, clock=clock.now, sleep=clock.sleep)
        result = art.for_track(_bluetooth())

    assert result is None


def test_transport_error_leaves_no_miss_then_a_later_call_fetches(tmp_path):
    state = {"fail": True}
    cover = _png_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "musicbrainz.org":
            if state["fail"]:
                raise httpx.ConnectError("boom")
            return httpx.Response(200, json={"releases": [{"id": MBID}]})
        if host == "coverartarchive.org":
            return httpx.Response(
                200, content=cover, headers={"content-type": "image/png"}
            )
        raise AssertionError(f"unexpected host {host}")

    clock = FakeClock()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        art = CoverArt(client, tmp_path, clock=clock.now, sleep=clock.sleep)
        first = art.for_track(_bluetooth())
        assert first is None
        assert list(tmp_path.glob("*.none")) == []
        state["fail"] = False
        second = art.for_track(_bluetooth())

    assert isinstance(second, Image.Image)


def test_server_error_leaves_no_miss_marker(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "musicbrainz.org"
        return httpx.Response(503)

    clock = FakeClock()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        art = CoverArt(client, tmp_path, clock=clock.now, sleep=clock.sleep)
        result = art.for_track(_bluetooth())

    assert result is None
    assert list(tmp_path.glob("*.none")) == []


def test_musicbrainz_gate_limits_to_one_request_per_second(tmp_path):
    clock = FakeClock()
    server = FakeServer(releases=[])
    with httpx.Client(transport=server.transport) as client:
        art = CoverArt(client, tmp_path, clock=clock.now, sleep=clock.sleep)
        art.for_track(_bluetooth(album="First"))
        art.for_track(_bluetooth(album="Second"))

    assert server.count("musicbrainz.org") == 2
    # The second search waited out the one-per-second gate without real sleeping.
    assert clock.sleeps == [1.0]


def test_art_url_with_non_http_scheme_is_refused(tmp_path):
    server = FakeServer(art_bytes=_png_bytes())
    np = NowPlaying(title="Tidal Drift", artist="Nova Reef", art_url="file:///etc/passwd")
    with httpx.Client(transport=server.transport) as client:
        result = CoverArt(client, tmp_path).for_track(np)

    assert result is None
    assert server.calls == []


def test_oversized_download_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(coverart, "_MAX_BYTES", 10)
    server = FakeServer(art_bytes=_png_bytes())
    with httpx.Client(transport=server.transport) as client:
        result = CoverArt(client, tmp_path).for_track(_airplay())

    assert result is None
    # The refused download left nothing cached.
    assert list(tmp_path.glob("*")) == []


def test_decompression_bomb_is_refused(tmp_path, monkeypatch):
    # Lower Pillow's pixel ceiling so a tiny test image trips the bomb guard.
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 10)
    server = FakeServer(art_bytes=_png_bytes())
    with httpx.Client(transport=server.transport) as client:
        result = CoverArt(client, tmp_path).for_track(_airplay())

    assert result is None


def test_image_download_disables_compression(tmp_path):
    server = FakeServer(art_bytes=_png_bytes())
    with httpx.Client(transport=server.transport) as client:
        CoverArt(client, tmp_path).for_track(_airplay())

    hosts = [host for host, _ in server.calls]
    idx = hosts.index("hub.local")
    # The image request opts out of content-encoding, so a small compressed body
    # cannot amplify into an unbounded decoded stream past the byte cap.
    assert server.accept_encodings[idx] == "identity"


def test_oversized_dimensions_are_refused_before_decode(tmp_path, monkeypatch):
    # An image whose declared dimensions exceed the cover-sized pixel cap is
    # rejected before Pillow decodes it, even when its byte size is small.
    monkeypatch.setattr(coverart, "_MAX_PIXELS", 10)
    server = FakeServer(art_bytes=_png_bytes())  # 8x8 = 64 px > 10
    with httpx.Client(transport=server.transport) as client:
        result = CoverArt(client, tmp_path).for_track(_airplay())

    assert result is None


def test_no_album_and_no_art_url_returns_none(tmp_path):
    server = FakeServer()
    with httpx.Client(transport=server.transport) as client:
        result = CoverArt(client, tmp_path).for_track(_bluetooth(album=None))

    assert result is None
    assert server.calls == []
