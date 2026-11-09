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
