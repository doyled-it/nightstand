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


def _jpeg(
    color: tuple[int, int, int] = (20, 40, 60), size: tuple[int, int] = (16, 8)
) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG")
    return buf.getvalue()


def _gray_png(values: list[int], size: tuple[int, int]) -> bytes:
    img = Image.new("L", size)
    img.putdata(values)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _faint_drawing_png() -> bytes:
    """A landscape image calibrated to the Met Klimt graphite "Two Women
    Reclining": darkest tone 169 of 255 on near-white paper, mean ~228. Its 2nd
    percentile stays above the faint floor, so it must be skipped."""
    w, h = 80, 48  # landscape, so the shape filter passes and faintness decides
    total = w * h
    dark = 120
    return _gray_png([169] * dark + [230] * (total - dark), (w, h))


def _ordinary_painting_png() -> bytes:
    """A landscape image with deep shadows (tones down to black), so its 2nd
    percentile is well below the faint floor and it is kept."""
    w, h = 80, 48
    total = w * h
    return _gray_png([(i * 255) // total for i in range(total)], (w, h))


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


def test_shape_skips_a_too_tall_image(tmp_path):
    # A portrait image (aspect below the 1.3 default) is downloaded, found too
    # tall, discarded, and counted under skipped_shape, not written.
    met = FakeMet(
        {11: _met_object(11, public=True, image=MET_IMG, title="Tall", artist="A")},
        image_body=_jpeg(size=(8, 16)),
    )
    with httpx.Client(transport=met.transport) as client:
        result = fetch_museum(client, "met", "x", tmp_path)

    assert result.written == 0
    assert result.skipped_shape == 1
    assert result.kept == 1
    # Nothing written and no stray .part file left behind.
    assert list(tmp_path.iterdir()) == []


def test_min_aspect_override_keeps_a_squarer_image(tmp_path):
    # A square image is below the 1.3 default but kept when the caller lowers the
    # minimum to 1.0.
    met = FakeMet(
        {11: _met_object(11, public=True, image=MET_IMG, title="Square", artist="A")},
        image_body=_jpeg(size=(8, 8)),
    )
    with httpx.Client(transport=met.transport) as client:
        result = fetch_museum(client, "met", "x", tmp_path, min_aspect=1.0)

    assert result.written == 1
    assert result.skipped_shape == 0
    assert (tmp_path / "met-11.jpg").exists()


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


# --- the faintness filter skips works that would vanish on the six-ink panel ---


def test_faint_image_is_skipped(tmp_path):
    # A graphite drawing on cream paper (darkest tone 169) reads as nearly blank
    # on the panel, so it is downloaded, judged too faint, discarded, and counted
    # under skipped_faint rather than written.
    met = FakeMet(
        {11: _met_object(11, public=True, image=MET_IMG, title="Faint", artist="A")},
        image_body=_faint_drawing_png(),
    )
    with httpx.Client(transport=met.transport) as client:
        result = fetch_museum(client, "met", "x", tmp_path, min_aspect=1.0)

    assert result.written == 0
    assert result.skipped_faint == 1
    assert result.kept == 1
    # Nothing written and no stray .part file left behind.
    assert list(tmp_path.iterdir()) == []


def test_ordinary_painting_is_not_skipped(tmp_path):
    # A painting with deep shadows is not faint and is kept.
    met = FakeMet(
        {11: _met_object(11, public=True, image=MET_IMG, title="Deep", artist="A")},
        image_body=_ordinary_painting_png(),
    )
    with httpx.Client(transport=met.transport) as client:
        result = fetch_museum(client, "met", "x", tmp_path, min_aspect=1.0)

    assert result.written == 1
    assert result.skipped_faint == 0
    assert (tmp_path / "met-11.jpg").exists()


def test_default_min_aspect_is_1_1(tmp_path):
    # A piece at aspect 1.2 is kept under the 1.1 default (the hub rotation
    # cutoff); it was discarded under the old 1.3 default.
    met = FakeMet(
        {11: _met_object(11, public=True, image=MET_IMG, title="Wide", artist="A")},
        image_body=_jpeg(size=(60, 50)),  # aspect 1.2
    )
    with httpx.Client(transport=met.transport) as client:
        result = fetch_museum(client, "met", "x", tmp_path)

    assert result.written == 1
    assert result.skipped_shape == 0


# --- Wikimedia Commons: public-domain paintings by a named artist ---

COMMONS_THUMB = (
    "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/"
    "Klimt.jpg/3000px-Klimt.jpg"
)


def _commons_page(
    pageid: int,
    title: str,
    *,
    pd: bool = True,
    thumb: str = COMMONS_THUMB,
    mime: str = "image/jpeg",
    object_name: str | None = None,
) -> dict:
    if pd:
        extmeta = {
            "LicenseShortName": {"value": "Public domain"},
            "License": {"value": "pd"},
        }
    else:
        extmeta = {
            "LicenseShortName": {"value": "CC BY-SA 4.0"},
            "License": {"value": "cc-by-sa-4.0"},
            "Copyrighted": {"value": "True"},
        }
    if object_name:
        extmeta["ObjectName"] = {"value": object_name}
    return {
        "pageid": pageid,
        "title": title,
        "imageinfo": [{"thumburl": thumb, "mime": mime, "extmetadata": extmeta}],
    }


class FakeCommons:
    """A MockTransport handler for the Commons API + the upload image host."""

    def __init__(
        self,
        pages: dict[str, dict],
        *,
        members: list[dict] | None = None,
        subcat_members: dict[str, list[dict]] | None = None,
        image_body: bytes | None = None,
    ) -> None:
        self.pages = pages
        self.members = members or []
        self.subcat_members = subcat_members or {}
        self.image_body = _jpeg() if image_body is None else image_body
        self.image_hits: list[str] = []
        self.api_requests: list[tuple[dict, httpx.Headers]] = []
        self.thumb_widths: list[str | None] = []

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host == "commons.wikimedia.org":
            params = dict(url.params)
            self.api_requests.append((params, request.headers))
            if params.get("list") == "categorymembers":
                cmtitle = params.get("cmtitle", "")
                members = self.subcat_members.get(cmtitle, self.members)
                return httpx.Response(
                    200, json={"query": {"categorymembers": members}}
                )
            if params.get("prop") == "imageinfo":
                self.thumb_widths.append(params.get("iiurlwidth"))
                titles = params.get("titles", "").split("|")
                pages = {
                    str(i): self.pages[t]
                    for i, t in enumerate(titles)
                    if t in self.pages
                }
                return httpx.Response(200, json={"query": {"pages": pages}})
            return httpx.Response(200, json={})
        if url.host == "upload.wikimedia.org":
            self.image_hits.append(str(url))
            return httpx.Response(200, content=self.image_body)
        return httpx.Response(404)


def test_commons_writes_public_domain_paintings_with_sidecars(tmp_path):
    members = [{"title": "File:Klimt - Kiss.jpg", "ns": 6}]
    pages = {
        "File:Klimt - Kiss.jpg": _commons_page(
            101, "File:Klimt - Kiss.jpg", object_name="The Kiss"
        )
    }
    commons = FakeCommons(pages, members=members)
    with httpx.Client(transport=commons.transport) as client:
        result = fetch_museum(client, "commons", "Gustav Klimt", tmp_path)

    assert result.written == 1
    assert (tmp_path / "commons-101.jpg").read_bytes() == _jpeg()
    assert (tmp_path / "commons-101.jpg.txt").read_text() == (
        "The Kiss\nGustav Klimt\n"
        "https://commons.wikimedia.org/wiki/File:Klimt_-_Kiss.jpg\n"
    )


def test_commons_title_falls_back_to_the_filename(tmp_path):
    members = [{"title": "File:Gustav Klimt 046.jpg", "ns": 6}]
    pages = {"File:Gustav Klimt 046.jpg": _commons_page(1, "File:Gustav Klimt 046.jpg")}
    commons = FakeCommons(pages, members=members)
    with httpx.Client(transport=commons.transport) as client:
        fetch_museum(client, "commons", "Gustav Klimt", tmp_path)

    # Without an ObjectName the title is the filename without the File: prefix
    # and extension.
    assert (tmp_path / "commons-1.jpg.txt").read_text().splitlines()[0] == (
        "Gustav Klimt 046"
    )


def test_commons_skips_non_public_domain(tmp_path):
    members = [{"title": "File:Modern.jpg", "ns": 6}]
    pages = {"File:Modern.jpg": _commons_page(102, "File:Modern.jpg", pd=False)}
    commons = FakeCommons(pages, members=members)
    with httpx.Client(transport=commons.transport) as client:
        result = fetch_museum(client, "commons", "Someone", tmp_path)

    assert result.written == 0
    assert list(tmp_path.glob("*.jpg")) == []
    assert commons.image_hits == []  # a non-PD file is never downloaded


def test_commons_requests_the_3000px_thumbnail(tmp_path):
    members = [{"title": "File:A.jpg", "ns": 6}]
    pages = {"File:A.jpg": _commons_page(1, "File:A.jpg")}
    commons = FakeCommons(pages, members=members)
    with httpx.Client(transport=commons.transport) as client:
        fetch_museum(client, "commons", "X", tmp_path)

    # The thumbnail URL API is used at a ~3000px width, not a full original.
    assert "3000" in commons.thumb_widths


def test_commons_sends_a_descriptive_user_agent(tmp_path):
    members = [{"title": "File:A.jpg", "ns": 6}]
    pages = {"File:A.jpg": _commons_page(1, "File:A.jpg")}
    commons = FakeCommons(pages, members=members)
    with httpx.Client(transport=commons.transport) as client:
        fetch_museum(client, "commons", "X", tmp_path)

    assert commons.api_requests, "the Commons API was not called"
    for _params, headers in commons.api_requests:
        ua = headers.get("User-Agent", "")
        assert "nightstand" in ua
        assert "github.com/doyled-it/nightstand" in ua


def test_commons_follows_subcategories(tmp_path):
    members = [{"title": "Category:Landscapes by X", "ns": 14}]
    subcat = {"Category:Landscapes by X": [{"title": "File:Hill.jpg", "ns": 6}]}
    pages = {"File:Hill.jpg": _commons_page(5, "File:Hill.jpg")}
    commons = FakeCommons(pages, members=members, subcat_members=subcat)
    with httpx.Client(transport=commons.transport) as client:
        result = fetch_museum(client, "commons", "X", tmp_path)

    assert result.written == 1
    assert (tmp_path / "commons-5.jpg").exists()


def test_commons_skips_a_drawing_by_title(tmp_path):
    members = [{"title": "File:Klimt drawing of a seated woman.jpg", "ns": 6}]
    pages = {
        "File:Klimt drawing of a seated woman.jpg": _commons_page(
            9, "File:Klimt drawing of a seated woman.jpg"
        )
    }
    commons = FakeCommons(pages, members=members)
    with httpx.Client(transport=commons.transport) as client:
        result = fetch_museum(client, "commons", "Klimt", tmp_path)

    assert result.written == 0
    assert commons.image_hits == []


def test_commons_rejects_image_from_an_unexpected_host(tmp_path):
    members = [{"title": "File:A.jpg", "ns": 6}]
    pages = {
        "File:A.jpg": _commons_page(
            1, "File:A.jpg", thumb="https://evil.example.com/x.jpg"
        )
    }
    commons = FakeCommons(pages, members=members)
    with httpx.Client(transport=commons.transport) as client:
        result = fetch_museum(client, "commons", "X", tmp_path)

    assert result.written == 0
    assert commons.image_hits == []


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
