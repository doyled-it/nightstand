from __future__ import annotations

import io

import httpx
import pytest
from PIL import Image

from nightstand.sources.museums import FetchResult, fetch_museum


def _jpeg(color: tuple[int, int, int] = (20, 40, 60)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buf, format="JPEG")
    return buf.getvalue()


class FakeMet:
    """A MockTransport handler for the Met search + object + image endpoints."""

    def __init__(self, objects: dict[int, dict]) -> None:
        self.objects = objects
        self.image_hits: list[int] = []

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host == "collectionapi.metmuseum.org":
            if url.path.endswith("/search"):
                return httpx.Response(
                    200,
                    json={"total": len(self.objects), "objectIDs": list(self.objects)},
                )
            object_id = int(url.path.rsplit("/", 1)[-1])
            return httpx.Response(200, json=self.objects[object_id])
        # The primaryImage lives on an image host.
        self.image_hits.append(1)
        return httpx.Response(200, content=_jpeg())


def _met_object(
    object_id: int, *, public: bool, image: str | None, title: str, artist: str
) -> dict:
    return {
        "objectID": object_id,
        "isPublicDomain": public,
        "primaryImage": image or "",
        "title": title,
        "artistDisplayName": artist,
    }


class FakeAic:
    """A MockTransport handler for the AIC search + IIIF image endpoints."""

    def __init__(self, data: list[dict]) -> None:
        self.data = data
        self.image_hits: list[str] = []

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host == "api.artic.edu":
            return httpx.Response(200, json={"data": self.data})
        # https://www.artic.edu/iiif/2/{image_id}/full/843,/0/default.jpg
        self.image_hits.append(url.path)
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
                image="http://img/11.jpg",
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
                11, public=True, image="http://img/11.jpg", title="Sea", artist="A"
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
            i, public=True, image=f"http://img/{i}.jpg", title=f"T{i}", artist="A"
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
        [_aic_item(7, public=True, image_id="abc", title="River", artist="B. Maker")]
    )
    with httpx.Client(transport=aic.transport) as client:
        result = fetch_museum(client, "aic", "river", tmp_path)

    assert result.written == 1
    assert (tmp_path / "aic-7.jpg").read_bytes() == _jpeg()
    assert (tmp_path / "aic-7.jpg.txt").read_text() == "River\nB. Maker\n"
    # The IIIF full-size image URL is used.
    assert aic.image_hits == ["/iiif/2/abc/full/843,/0/default.jpg"]


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


def test_sidecar_has_only_title_when_artist_is_missing(tmp_path):
    aic = FakeAic(
        [_aic_item(9, public=True, image_id="zz", title="Untitled", artist="")]
    )
    with httpx.Client(transport=aic.transport) as client:
        fetch_museum(client, "aic", "u", tmp_path)

    assert (tmp_path / "aic-9.jpg.txt").read_text() == "Untitled\n"


def test_unknown_source_is_rejected(tmp_path):
    transport = httpx.MockTransport(lambda r: httpx.Response(200))
    with httpx.Client(transport=transport) as client:
        with pytest.raises(ValueError):
            fetch_museum(client, "louvre", "x", tmp_path)


def test_result_equality_is_a_dataclass(tmp_path):
    assert FetchResult(written=1, skipped=2) == FetchResult(written=1, skipped=2)
