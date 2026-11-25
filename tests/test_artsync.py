from __future__ import annotations

import httpx

from nightstand.sources import artsync
from nightstand.sources.artsync import SyncResult, sync_index

INDEX_URL = "http://hub.local/art-index.json"
BASE_URL = "http://hub.local"

MTIME = 1794125000


def _piece(
    pid: str,
    *,
    folder: str,
    size: int,
    mtime: int = MTIME,
    title: str | None = "A Title",
    artist: str | None = None,
    source_url: str | None = None,
) -> dict:
    return {
        "id": pid,
        "folder": folder,
        "file": pid.rsplit("/", 1)[-1],
        "title": title,
        "artist": artist,
        "source_url": source_url,
        "width": 300,
        "height": 200,
        "landscape": True,
        "size": size,
        "mtime": mtime,
        "thumb": "/art-thumbs/x.jpg",
        "url": "/art/" + pid,
    }


def _index(pieces: list[dict]) -> dict:
    return {
        "generated_at": "2026-11-09T07:00:00-08:00",
        "folders": [],
        "pieces": pieces,
    }


class FakeHub:
    """A MockTransport handler serving an art-index and file bodies by url path."""

    def __init__(self, index: dict, files: dict[str, bytes]) -> None:
        self.index = index
        self.files = files
        self.index_calls = 0
        self.downloaded: list[str] = []

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/art-index.json":
            self.index_calls += 1
            return httpx.Response(200, json=self.index)
        if path in self.files:
            self.downloaded.append(path)
            return httpx.Response(200, content=self.files[path])
        return httpx.Response(404)


def _sync(hub: FakeHub, dest, enabled=None) -> SyncResult:
    with httpx.Client(transport=hub.transport) as client:
        return sync_index(client, INDEX_URL, BASE_URL, dest, enabled)


def test_adds_pieces_across_folders_and_writes_sidecars(tmp_path):
    hub = FakeHub(
        index=_index(
            [
                _piece("met-1.jpg", folder="Collection", size=3, title="Sunrise"),
                _piece(
                    "Photos/beach.jpg",
                    folder="Photos",
                    size=4,
                    title="Beach",
                    artist="Me",
                    source_url="https://example.org/1",
                ),
            ]
        ),
        files={"/art/met-1.jpg": b"abc", "/art/Photos/beach.jpg": b"abcd"},
    )

    result = _sync(hub, tmp_path)

    assert result == SyncResult(added=2, updated=0, removed=0)
    assert (tmp_path / "met-1.jpg").read_bytes() == b"abc"
    assert (tmp_path / "Photos" / "beach.jpg").read_bytes() == b"abcd"
    assert (tmp_path / "met-1.jpg.txt").read_text().splitlines()[0] == "Sunrise"
    beach_sidecar = (tmp_path / "Photos" / "beach.jpg.txt").read_text().splitlines()
    assert beach_sidecar[:3] == ["Beach", "Me", "https://example.org/1"]


def test_disabled_folder_is_skipped_and_its_local_pieces_removed(tmp_path):
    stale = tmp_path / "Photos" / "old.jpg"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"old")
    (tmp_path / "Photos" / "old.jpg.txt").write_text("Old\n")

    hub = FakeHub(
        index=_index(
            [
                _piece("met-1.jpg", folder="Collection", size=3),
                _piece("Photos/beach.jpg", folder="Photos", size=4),
            ]
        ),
        files={"/art/met-1.jpg": b"abc", "/art/Photos/beach.jpg": b"abcd"},
    )

    result = _sync(hub, tmp_path, enabled={"Collection"})

    assert (tmp_path / "met-1.jpg").exists()
    assert not (tmp_path / "Photos" / "beach.jpg").exists()
    assert not stale.exists()
    assert "/art/Photos/beach.jpg" not in hub.downloaded
    assert result.added == 1
    assert result.removed == 2  # the stale image and its sidecar
    assert not (tmp_path / "Photos").exists()  # the emptied folder is removed


def test_second_sync_downloads_nothing_when_unchanged(tmp_path):
    hub = FakeHub(
        index=_index([_piece("met-1.jpg", folder="Collection", size=3)]),
        files={"/art/met-1.jpg": b"abc"},
    )
    _sync(hub, tmp_path)
    hub.downloaded.clear()

    result = _sync(hub, tmp_path)

    assert result == SyncResult()
    assert hub.downloaded == []


def test_updates_a_piece_whose_size_or_mtime_changed(tmp_path):
    hub = FakeHub(
        index=_index([_piece("met-1.jpg", folder="Collection", size=3)]),
        files={"/art/met-1.jpg": b"abc"},
    )
    _sync(hub, tmp_path)

    hub.index = _index(
        [_piece("met-1.jpg", folder="Collection", size=5, mtime=MTIME + 10)]
    )
    hub.files = {"/art/met-1.jpg": b"abcde"}
    result = _sync(hub, tmp_path)

    assert result == SyncResult(added=0, updated=1, removed=0)
    assert (tmp_path / "met-1.jpg").read_bytes() == b"abcde"


def test_removes_a_piece_no_longer_listed(tmp_path):
    hub = FakeHub(
        index=_index(
            [
                _piece("met-1.jpg", folder="Collection", size=3),
                _piece("met-2.jpg", folder="Collection", size=3),
            ]
        ),
        files={"/art/met-1.jpg": b"abc", "/art/met-2.jpg": b"xyz"},
    )
    _sync(hub, tmp_path)

    hub.index = _index([_piece("met-1.jpg", folder="Collection", size=3)])
    result = _sync(hub, tmp_path)

    assert not (tmp_path / "met-2.jpg").exists()
    assert not (tmp_path / "met-2.jpg.txt").exists()
    assert (tmp_path / "met-1.jpg").exists()
    assert result.removed == 2  # the image and its sidecar


def test_piece_without_a_title_gets_no_sidecar(tmp_path):
    hub = FakeHub(
        index=_index([_piece("met-1.jpg", folder="Collection", size=3, title=None)]),
        files={"/art/met-1.jpg": b"abc"},
    )

    result = _sync(hub, tmp_path)

    assert (tmp_path / "met-1.jpg").exists()
    assert not (tmp_path / "met-1.jpg.txt").exists()
    assert result.added == 1


def test_rejects_traversal_and_deep_ids(tmp_path):
    hub = FakeHub(
        index=_index(
            [
                _piece("ok.jpg", folder="Collection", size=3),
                _piece("../evil.jpg", folder="Collection", size=3),
                _piece("a/b/c.jpg", folder="a", size=3),
                _piece(".trash/x.jpg", folder=".trash", size=3),
            ]
        ),
        files={
            "/art/ok.jpg": b"abc",
            "/art/../evil.jpg": b"abc",
            "/art/a/b/c.jpg": b"abc",
            "/art/.trash/x.jpg": b"abc",
        },
    )

    result = _sync(hub, tmp_path)

    assert result.added == 1
    assert (tmp_path / "ok.jpg").exists()
    assert not (tmp_path.parent / "evil.jpg").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["ok.jpg", "ok.jpg.txt"]


def test_bad_index_changes_nothing(tmp_path):
    kept = tmp_path / "keep.jpg"
    kept.write_bytes(b"keep")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>nope</html>")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = sync_index(client, INDEX_URL, BASE_URL, tmp_path, None)

    assert result == SyncResult()
    assert kept.read_bytes() == b"keep"


def test_missing_index_changes_nothing(tmp_path):
    kept = tmp_path / "keep.jpg"
    kept.write_bytes(b"keep")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = sync_index(client, INDEX_URL, BASE_URL, tmp_path, None)

    assert result == SyncResult()
    assert kept.exists()


def test_unreachable_hub_changes_nothing(tmp_path):
    kept = tmp_path / "keep.jpg"
    kept.write_bytes(b"keep")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("hub down")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = sync_index(client, INDEX_URL, BASE_URL, tmp_path, None)

    assert result == SyncResult()
    assert kept.exists()


def test_rejects_a_file_whose_declared_length_exceeds_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(artsync, "_MAX_FILE_BYTES", 4)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/art-index.json":
            return httpx.Response(
                200, json=_index([_piece("big.jpg", folder="Collection", size=100)])
            )
        return httpx.Response(
            200, content=b"x" * 100, headers={"content-length": "100"}
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = sync_index(client, INDEX_URL, BASE_URL, tmp_path, None)

    assert result.added == 0
    assert not (tmp_path / "big.jpg").exists()


def test_stops_and_discards_a_stream_past_the_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(artsync, "_MAX_FILE_BYTES", 4)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/art-index.json":
            return httpx.Response(
                200, json=_index([_piece("big.jpg", folder="Collection", size=3)])
            )
        return httpx.Response(200, content=b"xxxxxxxx")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = sync_index(client, INDEX_URL, BASE_URL, tmp_path, None)

    assert result.added == 0
    assert not (tmp_path / "big.jpg").exists()
    assert not (tmp_path / "big.jpg.part").exists()


def _ente_piece(pid: str, *, size: int, title: str = "Album One") -> dict:
    piece = _piece(pid, folder=pid.split("/")[0], size=size, title=title)
    piece["source"] = "ente"
    piece["landscape"] = False
    return piece


def test_ente_pieces_mirror_with_folder_layout_and_sidecar(tmp_path):
    hub = FakeHub(
        index=_index([_ente_piece("ente-101/5001.jpg", size=3)]),
        files={"/art/ente-101/5001.jpg": b"abc"},
    )

    result = _sync(hub, tmp_path, enabled={"ente-101"})

    assert result.added == 1
    assert (tmp_path / "ente-101" / "5001.jpg").read_bytes() == b"abc"
    sidecar = (tmp_path / "ente-101" / "5001.jpg.txt").read_text().splitlines()
    assert sidecar[0] == "Album One"


def test_ente_folder_switched_off_removes_its_files_and_dir(tmp_path):
    hub = FakeHub(
        index=_index(
            [
                _piece("met-1.jpg", folder="Collection", size=3),
                _ente_piece("ente-101/5001.jpg", size=3),
            ]
        ),
        files={"/art/met-1.jpg": b"abc", "/art/ente-101/5001.jpg": b"abc"},
    )
    _sync(hub, tmp_path, enabled={"Collection", "ente-101"})
    assert (tmp_path / "ente-101" / "5001.jpg").exists()

    result = _sync(hub, tmp_path, enabled={"Collection"})

    assert result.removed == 2  # the image and its sidecar
    assert not (tmp_path / "ente-101").exists()
    assert (tmp_path / "met-1.jpg").exists()


def test_removed_ente_piece_deleted_on_next_sync(tmp_path):
    hub = FakeHub(
        index=_index(
            [
                _ente_piece("ente-101/5001.jpg", size=3),
                _ente_piece("ente-101/5002.jpg", size=3),
            ]
        ),
        files={
            "/art/ente-101/5001.jpg": b"abc",
            "/art/ente-101/5002.jpg": b"abc",
        },
    )
    _sync(hub, tmp_path)

    hub.index = _index([_ente_piece("ente-101/5001.jpg", size=3)])
    result = _sync(hub, tmp_path)

    assert result.removed == 2
    assert (tmp_path / "ente-101" / "5001.jpg").exists()
    assert not (tmp_path / "ente-101" / "5002.jpg").exists()
    assert not (tmp_path / "ente-101" / "5002.jpg.txt").exists()


def test_index_with_unknown_source_and_ente_sync_keys_is_tolerated(tmp_path):
    index = _index([_ente_piece("ente-101/5001.jpg", size=3)])
    index["ente_sync"] = {"state": "failing", "last_success": None, "error": "x"}
    index["folders"] = [
        {
            "name": "ente-101",
            "count": 1,
            "enabled": True,
            "builtin": False,
            "source": "ente",
            "title": "Album One",
        },
        {"name": "Odd", "count": 0, "enabled": True, "source": "mystery"},
    ]
    odd = _piece("Odd/a.jpg", folder="Odd", size=3)
    odd["source"] = "mystery"
    odd["extra"] = {"nested": [1, 2]}
    index["pieces"].append(odd)
    hub = FakeHub(
        index=index,
        files={"/art/ente-101/5001.jpg": b"abc", "/art/Odd/a.jpg": b"abc"},
    )

    result = _sync(hub, tmp_path)

    assert result.added == 2
    assert (tmp_path / "ente-101" / "5001.jpg").exists()
    assert (tmp_path / "Odd" / "a.jpg").exists()


def test_unsafe_ente_looking_id_still_refused(tmp_path):
    hub = FakeHub(
        index=_index(
            [
                _ente_piece("ente-101/5001.jpg", size=3),
                _ente_piece("ente-x/../a.jpg", size=3),
            ]
        ),
        files={
            "/art/ente-101/5001.jpg": b"abc",
            "/art/ente-x/../a.jpg": b"abc",
            "/art/a.jpg": b"abc",
        },
    )

    result = _sync(hub, tmp_path)

    assert result.added == 1
    assert not (tmp_path / "a.jpg").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["ente-101"]


# --- crop boxes ----------------------------------------------------------- #

BOX = {"x": 0.1, "y": 0.05, "w": 0.6, "h": 0.6}
AUTO = {"x": 0.0, "y": 0.0, "w": 1.0, "h": 0.5}


def _sync_boxes(hub: FakeHub, dest, crops_path, enabled=None) -> SyncResult:
    with httpx.Client(transport=hub.transport) as client:
        return sync_index(
            client, INDEX_URL, BASE_URL, dest, enabled, crops_path=crops_path
        )


def _loaded(crops_path):
    from nightstand.crops import CropMap

    return CropMap(crops_path).load()


def test_sync_keeps_each_pieces_crop_and_auto_crop(tmp_path):
    crops = tmp_path / "state" / "art-crops.json"
    dest = tmp_path / "art"
    plain = _piece("met-1.jpg", folder="Collection", size=3)
    boxed = _piece("Photos/beach.jpg", folder="Photos", size=4)
    boxed["crop"] = BOX
    boxed["auto_crop"] = AUTO
    hub = FakeHub(
        index=_index([plain, boxed]),
        files={"/art/met-1.jpg": b"abc", "/art/Photos/beach.jpg": b"abcd"},
    )

    _sync_boxes(hub, dest, crops)

    loaded = _loaded(crops)
    assert loaded["Photos/beach.jpg"] == ((0.1, 0.05, 0.6, 0.6), (0.0, 0.0, 1.0, 0.5))
    # An older hub that sends neither key: the piece is listed with no boxes.
    assert loaded["met-1.jpg"] == (None, None)


def test_sync_ignores_a_malformed_box_but_keeps_the_piece(tmp_path):
    crops = tmp_path / "art-crops.json"
    piece = _piece("a.jpg", folder="Collection", size=3)
    piece["crop"] = {"x": "0", "y": 0, "w": 1, "h": 1}
    piece["auto_crop"] = [1, 2]
    hub = FakeHub(index=_index([piece]), files={"/art/a.jpg": b"abc"})

    _sync_boxes(hub, tmp_path / "art", crops)

    assert _loaded(crops) == {"a.jpg": (None, None)}


def test_sync_updates_a_changed_box_and_clears_a_removed_one(tmp_path):
    crops = tmp_path / "art-crops.json"
    dest = tmp_path / "art"
    piece = _piece("a.jpg", folder="Collection", size=3)
    piece["crop"] = BOX
    hub = FakeHub(index=_index([piece]), files={"/art/a.jpg": b"abc"})
    _sync_boxes(hub, dest, crops)
    assert _loaded(crops)["a.jpg"][0] == (0.1, 0.05, 0.6, 0.6)

    piece["crop"] = None
    piece["auto_crop"] = AUTO
    _sync_boxes(hub, dest, crops)
    assert _loaded(crops)["a.jpg"] == (None, (0.0, 0.0, 1.0, 0.5))


def test_sync_prunes_boxes_for_pieces_that_are_gone_or_disabled(tmp_path):
    crops = tmp_path / "art-crops.json"
    dest = tmp_path / "art"
    keep = _piece("a.jpg", folder="Collection", size=3)
    gone = _piece("b.jpg", folder="Collection", size=3)
    off = _piece("Off/c.jpg", folder="Off", size=3)
    for piece in (keep, gone, off):
        piece["crop"] = BOX
    hub = FakeHub(
        index=_index([keep, gone, off]),
        files={"/art/a.jpg": b"abc", "/art/b.jpg": b"abc", "/art/Off/c.jpg": b"abc"},
    )
    _sync_boxes(hub, dest, crops)
    assert set(_loaded(crops)) == {"a.jpg", "b.jpg", "Off/c.jpg"}

    hub.index = _index([keep, gone, off])
    hub.index["pieces"].remove(gone)
    _sync_boxes(hub, dest, crops, enabled={"Collection"})
    assert set(_loaded(crops)) == {"a.jpg"}


def test_sync_leaves_the_boxes_alone_when_the_index_is_unusable(tmp_path):
    crops = tmp_path / "art-crops.json"
    dest = tmp_path / "art"
    piece = _piece("a.jpg", folder="Collection", size=3)
    piece["crop"] = BOX
    hub = FakeHub(index=_index([piece]), files={"/art/a.jpg": b"abc"})
    _sync_boxes(hub, dest, crops)

    hub.index = {"nope": True}
    _sync_boxes(hub, dest, crops)
    assert _loaded(crops)["a.jpg"][0] == (0.1, 0.05, 0.6, 0.6)


def test_sync_survives_an_unwritable_boxes_file(tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("x")
    piece = _piece("a.jpg", folder="Collection", size=3)
    hub = FakeHub(index=_index([piece]), files={"/art/a.jpg": b"abc"})

    result = _sync_boxes(hub, tmp_path / "art", blocked / "art-crops.json")

    assert result.added == 1
