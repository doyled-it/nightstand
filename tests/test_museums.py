from __future__ import annotations

import io
import json
import os
from pathlib import Path

import httpx
import pytest
from PIL import Image

from nightstand.sources.museums import FetchResult, fetch_museum

_FIXTURES = Path(__file__).parent / "fixtures"

# The identification the Art Institute asks clients to send, app name and a
# contact URL, with no email per the privacy rule.
AIC_IDENTITY = "nightstand (https://github.com/doyled-it/nightstand)"


def _jpeg(color: tuple[int, int, int] = (20, 40, 60)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buf, format="JPEG")
    return buf.getvalue()


# A valid Met primaryImage URL (the host the validator trusts) and a valid AIC
# image_id (a UUID the validator accepts for interpolation into the IIIF URL).
MET_IMG = "https://images.metmuseum.org/CRDImages/ep/original/DP-11.jpg"
AIC_IMG_ID = "1adf2696-8489-499b-cad2-821d7fde4b33"


class FakeMet:
    """A MockTransport handler for the Met search + object + image endpoints."""

    def __init__(
        self, objects: dict[int, dict], image_body: bytes | None = None
    ) -> None:
        self.objects = objects
        self.image_body = _jpeg() if image_body is None else image_body
        self.image_hits: list[str] = []
        self.search_paths: list[str] = []
        self.object_paths: list[str] = []

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host == "collectionapi.metmuseum.org":
            if url.path.endswith("/search"):
                self.search_paths.append(url.path)
                return httpx.Response(
                    200,
                    json={"total": len(self.objects), "objectIDs": list(self.objects)},
                )
            self.object_paths.append(url.path)
            object_id = int(url.path.rsplit("/", 1)[-1])
            return httpx.Response(200, json=self.objects[object_id])
        # The primaryImage lives on an image host.
        self.image_hits.append(str(url))
        return httpx.Response(200, content=self.image_body)


def _met_object(
    object_id: int,
    *,
    public: bool,
    image: str | None,
    title: str,
    artist: str,
    object_url: str | None = None,
) -> dict:
    return {
        "objectID": object_id,
        "isPublicDomain": public,
        "primaryImage": image or "",
        "title": title,
        "artistDisplayName": artist,
        "objectURL": object_url,
    }


class FakeAic:
    """A MockTransport handler for the AIC search + IIIF image endpoints."""

    def __init__(self, data: list[dict]) -> None:
        self.data = data
        self.image_hits: list[str] = []
        self.api_headers: list[httpx.Headers] = []
        self.image_headers: list[httpx.Headers] = []

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host == "api.artic.edu":
            self.api_headers.append(request.headers)
            return httpx.Response(200, json={"data": self.data})
        # https://www.artic.edu/iiif/2/{image_id}/full/843,/0/default.jpg
        self.image_hits.append(url.path)
        self.image_headers.append(request.headers)
        return httpx.Response(200, content=_jpeg())


def _aic_item(
    item_id: int, *, public: bool, image_id: str | None, title: str, artist: str
) -> dict:
    return {
        "id": item_id,
        "title": title,
        "artist_title": artist,
        "image_id": image_id,
        "is_public_domain": public,
    }


def test_met_writes_public_domain_items_with_sidecars(tmp_path):
    met = FakeMet(
        {
            11: _met_object(
                11,
                public=True,
                image=MET_IMG,
                title="Sea",
                artist="A. Painter",
            )
        }
    )
    with httpx.Client(transport=met.transport) as client:
        result = fetch_museum(client, "met", "sea", tmp_path)

    assert result.written == 1
    assert (tmp_path / "met-11.jpg").read_bytes() == _jpeg()
    assert (tmp_path / "met-11.jpg.txt").read_text() == "Sea\nA. Painter\n"


def test_met_skips_non_public_domain_and_imageless(tmp_path):
    met = FakeMet(
        {
            11: _met_object(
                11, public=False, image="http://img/11.jpg", title="X", artist="Y"
            ),
            12: _met_object(12, public=True, image=None, title="No Image", artist="Z"),
        }
    )
    with httpx.Client(transport=met.transport) as client:
        result = fetch_museum(client, "met", "x", tmp_path)

    assert result.written == 0
    assert list(tmp_path.glob("*.jpg")) == []


def test_met_rerun_downloads_nothing_new(tmp_path):
    met = FakeMet(
        {
            11: _met_object(
                11, public=True, image=MET_IMG, title="Sea", artist="A"
            )
        }
    )
    with httpx.Client(transport=met.transport) as client:
        fetch_museum(client, "met", "sea", tmp_path)
        before = len(met.image_hits)
        result = fetch_museum(client, "met", "sea", tmp_path)

    assert result.written == 0
    assert result.skipped == 1
    assert len(met.image_hits) == before  # no second image download


def test_met_limit_caps_the_number_written(tmp_path):
    objects = {
        i: _met_object(
            i,
            public=True,
            image=f"https://images.metmuseum.org/CRDImages/{i}.jpg",
            title=f"T{i}",
            artist="A",
        )
        for i in range(1, 6)
    }
    met = FakeMet(objects)
    with httpx.Client(transport=met.transport) as client:
        result = fetch_museum(client, "met", "many", tmp_path, limit=2)

    assert result.written == 2
    assert len(list(tmp_path.glob("*.jpg"))) == 2


def test_aic_writes_public_domain_items_with_sidecars(tmp_path):
    aic = FakeAic(
        [
            _aic_item(
                7, public=True, image_id=AIC_IMG_ID, title="River", artist="B. Maker"
            )
        ]
    )
    with httpx.Client(transport=aic.transport) as client:
        result = fetch_museum(client, "aic", "river", tmp_path)

    assert result.written == 1
    assert (tmp_path / "aic-7.jpg").read_bytes() == _jpeg()
    assert (tmp_path / "aic-7.jpg.txt").read_text() == (
        "River\nB. Maker\nhttps://www.artic.edu/artworks/7\n"
    )
    # The IIIF full-size image URL is used.
    assert aic.image_hits == [f"/iiif/2/{AIC_IMG_ID}/full/843,/0/default.jpg"]


def test_aic_skips_non_public_domain_and_imageless(tmp_path):
    aic = FakeAic(
        [
            _aic_item(7, public=False, image_id="abc", title="X", artist="Y"),
            _aic_item(8, public=True, image_id=None, title="No Image", artist="Z"),
        ]
    )
    with httpx.Client(transport=aic.transport) as client:
        result = fetch_museum(client, "aic", "x", tmp_path)

    assert result.written == 0
    assert list(tmp_path.glob("*.jpg")) == []


def test_sidecar_has_only_title_when_artist_and_url_are_missing(tmp_path):
    # A Met object with no artist and no object URL yields a one-line sidecar.
    met = FakeMet(
        {
            9: _met_object(
                9, public=True, image=MET_IMG, title="Untitled", artist=""
            )
        }
    )
    with httpx.Client(transport=met.transport) as client:
        fetch_museum(client, "met", "u", tmp_path)

    assert (tmp_path / "met-9.jpg.txt").read_text() == "Untitled\n"


# --- the sidecar's optional third line is the source page URL ---


def test_met_sidecar_third_line_is_the_object_url(tmp_path):
    met = FakeMet(
        {
            11: _met_object(
                11,
                public=True,
                image=MET_IMG,
                title="Sea",
                artist="A. Painter",
                object_url="https://www.metmuseum.org/art/collection/search/11",
            )
        }
    )
    with httpx.Client(transport=met.transport) as client:
        fetch_museum(client, "met", "sea", tmp_path)

    assert (tmp_path / "met-11.jpg.txt").read_text() == (
        "Sea\nA. Painter\nhttps://www.metmuseum.org/art/collection/search/11\n"
    )


def test_met_sidecar_omits_a_non_https_object_url(tmp_path):
    met = FakeMet(
        {
            11: _met_object(
                11,
                public=True,
                image=MET_IMG,
                title="Sea",
                artist="A. Painter",
                object_url="http://www.metmuseum.org/art/collection/search/11",
            )
        }
    )
    with httpx.Client(transport=met.transport) as client:
        fetch_museum(client, "met", "sea", tmp_path)

    # A non-https object URL is dropped; the sidecar falls back to two lines.
    assert (tmp_path / "met-11.jpg.txt").read_text() == "Sea\nA. Painter\n"


def test_aic_sidecar_third_line_is_the_artwork_page(tmp_path):
    aic = FakeAic(
        [_aic_item(7, public=True, image_id=AIC_IMG_ID, title="River", artist="B")]
    )
    with httpx.Client(transport=aic.transport) as client:
        fetch_museum(client, "aic", "river", tmp_path)

    assert (tmp_path / "aic-7.jpg.txt").read_text() == (
        "River\nB\nhttps://www.artic.edu/artworks/7\n"
    )


def test_sidecar_keeps_the_url_on_line_three_when_artist_is_missing(tmp_path):
    # The artist line is left blank so the URL is always the third line.
    aic = FakeAic(
        [_aic_item(9, public=True, image_id=AIC_IMG_ID, title="Untitled", artist="")]
    )
    with httpx.Client(transport=aic.transport) as client:
        fetch_museum(client, "aic", "u", tmp_path)

    assert (tmp_path / "aic-9.jpg.txt").read_text() == (
        "Untitled\n\nhttps://www.artic.edu/artworks/9\n"
    )


def test_unknown_source_is_rejected(tmp_path):
    transport = httpx.MockTransport(lambda r: httpx.Response(200))
    with httpx.Client(transport=transport) as client:
        with pytest.raises(ValueError):
            fetch_museum(client, "louvre", "x", tmp_path)


def test_result_equality_is_a_dataclass(tmp_path):
    assert FetchResult(written=1, skipped=2) == FetchResult(written=1, skipped=2)


def test_met_rejects_image_from_an_unexpected_host(tmp_path):
    # A compromised Met response could point primaryImage at an internal address.
    met = FakeMet(
        {
            11: _met_object(
                11,
                public=True,
                image="http://169.254.169.254/latest/meta-data/",
                title="SSRF",
                artist="X",
            )
        }
    )
    with httpx.Client(transport=met.transport) as client:
        result = fetch_museum(client, "met", "x", tmp_path)

    assert result.written == 0
    assert met.image_hits == []  # the bad URL was never requested
    assert list(tmp_path.glob("*.jpg")) == []


def test_met_rejects_plain_http_image_url(tmp_path):
    met = FakeMet(
        {
            11: _met_object(
                11,
                public=True,
                image="http://images.metmuseum.org/x.jpg",  # not https
                title="Insecure",
                artist="X",
            )
        }
    )
    with httpx.Client(transport=met.transport) as client:
        result = fetch_museum(client, "met", "x", tmp_path)

    assert result.written == 0
    assert met.image_hits == []


def test_aic_rejects_non_uuid_image_id(tmp_path):
    aic = FakeAic(
        [_aic_item(7, public=True, image_id="../../etc/passwd", title="X", artist="Y")]
    )
    with httpx.Client(transport=aic.transport) as client:
        result = fetch_museum(client, "aic", "x", tmp_path)

    assert result.written == 0
    assert aic.image_hits == []  # nothing interpolated into the IIIF URL
    assert list(tmp_path.glob("*.jpg")) == []


def test_download_rejects_a_non_image_body(tmp_path):
    met = FakeMet(
        {11: _met_object(11, public=True, image=MET_IMG, title="T", artist="A")},
        image_body=b"<html>not an image</html>",
    )
    with httpx.Client(transport=met.transport) as client:
        result = fetch_museum(client, "met", "x", tmp_path)

    assert result.written == 0
    # Nothing written and no stray .part file left behind.
    assert list(tmp_path.iterdir()) == []


def test_download_rejects_an_oversized_body(tmp_path, monkeypatch):
    from nightstand.sources import museums

    monkeypatch.setattr(museums, "_MAX_BYTES", 4)
    met = FakeMet(
        {11: _met_object(11, public=True, image=MET_IMG, title="T", artist="A")},
        image_body=_jpeg() * 10,
    )
    with httpx.Client(transport=met.transport) as client:
        result = fetch_museum(client, "met", "x", tmp_path)

    assert result.written == 0
    assert list(tmp_path.iterdir()) == []


# --- Met search moved to the v1.1 endpoint; object lookups stay on v1 ---


def test_met_search_uses_the_v1_1_endpoint(tmp_path):
    # The v1 search was retired on 2026-10-01 (it now returns 410). Search must
    # hit /public/collection/v1.1/search.
    met = FakeMet(
        {11: _met_object(11, public=True, image=MET_IMG, title="Sea", artist="A")}
    )
    with httpx.Client(transport=met.transport) as client:
        fetch_museum(client, "met", "sea", tmp_path)

    assert met.search_paths == ["/public/collection/v1.1/search"]


def test_met_object_lookup_stays_on_v1(tmp_path):
    # There is no v1.1 object endpoint (it answers 404); objects stay on v1.
    met = FakeMet(
        {11: _met_object(11, public=True, image=MET_IMG, title="Sea", artist="A")}
    )
    with httpx.Client(transport=met.transport) as client:
        fetch_museum(client, "met", "sea", tmp_path)

    assert met.object_paths == ["/public/collection/v1/objects/11"]


# --- AIC identification: AIC-User-Agent header, library-default User-Agent ---


def test_aic_requests_send_the_aic_user_agent_header(tmp_path):
    aic = FakeAic(
        [_aic_item(7, public=True, image_id=AIC_IMG_ID, title="River", artist="B")]
    )
    with httpx.Client(transport=aic.transport) as client:
        fetch_museum(client, "aic", "river", tmp_path)

    assert aic.api_headers, "the search endpoint was not called"
    assert aic.image_headers, "the image endpoint was not called"
    for headers in aic.api_headers + aic.image_headers:
        assert headers.get("AIC-User-Agent") == AIC_IDENTITY


def test_aic_does_not_send_the_nightstand_user_agent(tmp_path):
    # AIC's image CDN answers 403 to our "nightstand/<version> (<repo>)"
    # User-Agent. The request must fall back to the library default instead of
    # spoofing a browser.
    aic = FakeAic(
        [_aic_item(7, public=True, image_id=AIC_IMG_ID, title="River", artist="B")]
    )
    with httpx.Client(transport=aic.transport) as client:
        fetch_museum(client, "aic", "river", tmp_path)

    for headers in aic.api_headers + aic.image_headers:
        ua = headers.get("User-Agent", "")
        assert "nightstand" not in ua
        assert ua.startswith("python-httpx/")


# --- the fetch reports found, kept, written, and skipped ---


def test_result_reports_found_kept_written_skipped(tmp_path):
    met = FakeMet(
        {
            11: _met_object(11, public=True, image=MET_IMG, title="A", artist="X"),
            12: _met_object(12, public=False, image=MET_IMG, title="B", artist="Y"),
            13: _met_object(13, public=True, image=None, title="C", artist="Z"),
        }
    )
    with httpx.Client(transport=met.transport) as client:
        result = fetch_museum(client, "met", "x", tmp_path)

    assert result.found == 3  # three search hits examined
    assert result.kept == 1  # one public-domain work with an image
    assert result.written == 1
    assert result.skipped == 0


def test_result_counts_a_rerun_as_skipped_not_written(tmp_path):
    met = FakeMet(
        {11: _met_object(11, public=True, image=MET_IMG, title="A", artist="X")}
    )
    with httpx.Client(transport=met.transport) as client:
        fetch_museum(client, "met", "x", tmp_path)
        result = fetch_museum(client, "met", "x", tmp_path)

    assert result.found == 1
    assert result.kept == 1
    assert result.written == 0
    assert result.skipped == 1


# --- the parsers handle the recorded real responses ---


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def test_met_parses_the_recorded_fixture(tmp_path):
    search = _load(_FIXTURES / "met" / "search.json")
    objects = {
        obj["objectID"]: obj
        for obj in (
            _load(p) for p in sorted((_FIXTURES / "met").glob("object_*.json"))
        )
    }

    def handle(request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host == "collectionapi.metmuseum.org":
            if url.path.endswith("/search"):
                return httpx.Response(200, json=search)
            return httpx.Response(200, json=objects[int(url.path.rsplit("/", 1)[-1])])
        return httpx.Response(200, content=_jpeg())

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = fetch_museum(client, "met", "van gogh", tmp_path)

    assert result.found == len(search["objectIDs"])
    assert result.written == len(objects)
    first = min(objects)
    assert (tmp_path / f"met-{first}.jpg").exists()
    sidecar = (tmp_path / f"met-{first}.jpg.txt").read_text()
    assert objects[first]["title"] in sidecar


def test_aic_parses_the_recorded_fixture(tmp_path):
    search = _load(_FIXTURES / "aic" / "search.json")

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.artic.edu":
            return httpx.Response(200, json=search)
        return httpx.Response(200, content=_jpeg())

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = fetch_museum(client, "aic", "van gogh", tmp_path)

    public = [e for e in search["data"] if e["is_public_domain"] and e["image_id"]]
    assert result.found == len(search["data"])
    assert result.written == len(public)
    first = public[0]
    assert (tmp_path / f"aic-{first['id']}.jpg").exists()


# --- opt-in live smoke test, skipped unless NIGHTSTAND_LIVE_MUSEUMS is set ---


@pytest.mark.skipif(
    not os.environ.get("NIGHTSTAND_LIVE_MUSEUMS"),
    reason="set NIGHTSTAND_LIVE_MUSEUMS=1 to run the live museum smoke test",
)
@pytest.mark.parametrize("source", ["met", "aic"])
def test_live_fetch_writes_at_least_one_work(source, tmp_path):
    # Hits the real APIs. AIC's image CDN challenges some VPN egress IPs, so run
    # this from a host that is not behind a VPN (e.g. the prototype Pi).
    with httpx.Client() as client:
        result = fetch_museum(client, source, "van gogh", tmp_path, limit=2)

    assert result.found > 0
    assert result.written >= 1
