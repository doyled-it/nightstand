from __future__ import annotations

import importlib.metadata
import io

import httpx
from PIL import Image

from nightstand.models import NowPlaying
from nightstand.sources import coverart
from nightstand.sources.coverart import USER_AGENT, CoverArt

HUB_BASE = "http://house.local"


def _png_bytes(color: tuple[int, int, int] = (10, 20, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buf, format="PNG")
    return buf.getvalue()


class FakeHub:
    """A MockTransport handler that records calls and serves canned art bytes."""

    def __init__(self, *, art_bytes: bytes | None = None) -> None:
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
        self.calls.append((request.url.host, request.url.path))
        self.user_agents.append(request.headers.get("user-agent"))
        self.accept_encodings.append(request.headers.get("accept-encoding"))
        if self.art_bytes is None:
            return httpx.Response(404)
        return httpx.Response(200, content=self.art_bytes)


def _with_cover(art_url: str | None = "/cover/3f2a9b7c.jpg") -> NowPlaying:
    return NowPlaying(
        title="Tidal Drift",
        artist="Nova Reef",
        album="Blue Hour",
        art_url=art_url,
    )


def _no_cover() -> NowPlaying:
    # Bluetooth and some apps send no cover; the frame no longer looks one up.
    return NowPlaying(title="Tidal Drift", artist="Nova Reef", album="Blue Hour")


def test_art_url_path_fetches_from_hub_once_then_reads_cache(tmp_path):
    hub = FakeHub(art_bytes=_png_bytes())
    with httpx.Client(transport=hub.transport) as client:
        art = CoverArt(client, tmp_path, HUB_BASE)
        first = art.for_track(_with_cover())
        second = art.for_track(_with_cover())

    assert isinstance(first, Image.Image)
    assert isinstance(second, Image.Image)
    # The same-origin path resolved against the hub base, and only once.
    assert hub.calls[0] == ("house.local", "/cover/3f2a9b7c.jpg")
    assert hub.count("house.local") == 1


def test_no_art_url_returns_none_without_any_request(tmp_path):
    hub = FakeHub(art_bytes=_png_bytes())
    with httpx.Client(transport=hub.transport) as client:
        result = CoverArt(client, tmp_path, HUB_BASE).for_track(_no_cover())

    assert result is None
    assert hub.calls == []


def test_no_musicbrainz_request_is_ever_made(tmp_path):
    # The hub does all artwork lookups now; the frame only fetches the hub's URL.
    hub = FakeHub(art_bytes=_png_bytes())
    with httpx.Client(transport=hub.transport) as client:
        CoverArt(client, tmp_path, HUB_BASE).for_track(_with_cover())
        CoverArt(client, tmp_path, HUB_BASE).for_track(_no_cover())

    hosts = {host for host, _ in hub.calls}
    assert "musicbrainz.org" not in hosts
    assert "coverartarchive.org" not in hosts


def test_missing_cover_returns_none_and_caches_nothing(tmp_path):
    hub = FakeHub(art_bytes=None)  # the hub 404s the path
    with httpx.Client(transport=hub.transport) as client:
        result = CoverArt(client, tmp_path, HUB_BASE).for_track(_with_cover())

    assert result is None
    assert list(tmp_path.glob("*")) == []


def test_user_agent_is_exact_and_has_no_at_sign(tmp_path):
    hub = FakeHub(art_bytes=_png_bytes())
    with httpx.Client(transport=hub.transport) as client:
        CoverArt(client, tmp_path, HUB_BASE).for_track(_with_cover())

    assert hub.user_agents, "expected at least one outbound request"
    assert all(ua == USER_AGENT for ua in hub.user_agents)
    assert "@" not in USER_AGENT


def test_user_agent_matches_the_package_version(tmp_path):
    version = importlib.metadata.version("nightstand")
    assert USER_AGENT == (
        f"nightstand/{version} ( https://github.com/doyled-it/nightstand )"
    )


def test_user_agent_falls_back_to_unknown_without_package_metadata(monkeypatch):
    def raise_missing(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(coverart, "_user_agent_cache", None)
    monkeypatch.setattr(importlib.metadata, "version", raise_missing)

    ua = coverart._user_agent()

    assert ua == "nightstand/unknown ( https://github.com/doyled-it/nightstand )"


def test_non_http_art_url_is_refused(tmp_path):
    # art_url should be a same-origin path, but a sender-supplied absolute URL
    # with a dangerous scheme must never be fetched.
    np = NowPlaying(title="T", artist="A", art_url="file:///etc/passwd")
    hub = FakeHub(art_bytes=_png_bytes())
    with httpx.Client(transport=hub.transport) as client:
        result = CoverArt(client, tmp_path, HUB_BASE).for_track(np)

    assert result is None
    assert hub.calls == []


def test_oversized_download_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(coverart, "_MAX_BYTES", 10)
    hub = FakeHub(art_bytes=_png_bytes())
    with httpx.Client(transport=hub.transport) as client:
        result = CoverArt(client, tmp_path, HUB_BASE).for_track(_with_cover())

    assert result is None
    assert list(tmp_path.glob("*")) == []


def test_decompression_bomb_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 10)
    hub = FakeHub(art_bytes=_png_bytes())
    with httpx.Client(transport=hub.transport) as client:
        result = CoverArt(client, tmp_path, HUB_BASE).for_track(_with_cover())

    assert result is None


def test_oversized_dimensions_are_refused_before_decode(tmp_path, monkeypatch):
    monkeypatch.setattr(coverart, "_MAX_PIXELS", 10)
    hub = FakeHub(art_bytes=_png_bytes())  # 8x8 = 64 px > 10
    with httpx.Client(transport=hub.transport) as client:
        result = CoverArt(client, tmp_path, HUB_BASE).for_track(_with_cover())

    assert result is None


def test_image_download_disables_compression(tmp_path):
    hub = FakeHub(art_bytes=_png_bytes())
    with httpx.Client(transport=hub.transport) as client:
        CoverArt(client, tmp_path, HUB_BASE).for_track(_with_cover())

    assert hub.accept_encodings[0] == "identity"


def test_transport_error_returns_none_then_a_later_call_fetches(tmp_path):
    state = {"fail": True}
    cover = _png_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if state["fail"]:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, content=cover)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        art = CoverArt(client, tmp_path, HUB_BASE)
        first = art.for_track(_with_cover())
        assert first is None
        assert list(tmp_path.glob("*")) == []
        state["fail"] = False
        second = art.for_track(_with_cover())

    assert isinstance(second, Image.Image)
