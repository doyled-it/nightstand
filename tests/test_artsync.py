from __future__ import annotations

import httpx

from nightstand.sources import artsync
from nightstand.sources.artsync import SyncResult, sync_art

BASE_URL = "http://hub.local/art/"

# nginx autoindex_format json serves mtime as an HTTP-date in GMT.
MTIME_A = "Mon, 02 Nov 2026 10:00:00 GMT"
MTIME_B = "Tue, 03 Nov 2026 11:00:00 GMT"


def _entry(name: str, mtime: str, size: int, type: str = "file") -> dict:
    return {"name": name, "type": type, "mtime": mtime, "size": size}


class FakeHub:
    """A MockTransport handler serving an autoindex listing and file bodies."""

    def __init__(self, listing: list[dict], files: dict[str, bytes]) -> None:
        self.listing = listing
        self.files = files
        self.listing_calls = 0
        self.downloaded: list[str] = []

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/art/":
            self.listing_calls += 1
            return httpx.Response(200, json=self.listing)
        name = path.rsplit("/", 1)[-1]
        if name in self.files:
            self.downloaded.append(name)
            return httpx.Response(200, content=self.files[name])
        return httpx.Response(404)


def test_adds_new_files_from_the_listing(tmp_path):
    hub = FakeHub(
        listing=[_entry("one.jpg", MTIME_A, 3), _entry("one.jpg.txt", MTIME_A, 5)],
        files={"one.jpg": b"abc", "one.jpg.txt": b"Title"},
    )
    with httpx.Client(transport=hub.transport) as client:
        result = sync_art(client, BASE_URL, tmp_path)

    assert result == SyncResult(added=2, updated=0, removed=0)
    assert (tmp_path / "one.jpg").read_bytes() == b"abc"
    assert (tmp_path / "one.jpg.txt").read_bytes() == b"Title"


def test_second_sync_downloads_nothing_when_unchanged(tmp_path):
    hub = FakeHub(
        listing=[_entry("one.jpg", MTIME_A, 3)],
        files={"one.jpg": b"abc"},
    )
    with httpx.Client(transport=hub.transport) as client:
        sync_art(client, BASE_URL, tmp_path)
        hub.downloaded.clear()
        result = sync_art(client, BASE_URL, tmp_path)

    assert result == SyncResult(added=0, updated=0, removed=0)
    assert hub.downloaded == []


def test_updates_a_file_whose_size_or_mtime_changed(tmp_path):
    hub = FakeHub(
        listing=[_entry("one.jpg", MTIME_A, 3)],
        files={"one.jpg": b"abc"},
    )
    with httpx.Client(transport=hub.transport) as client:
        sync_art(client, BASE_URL, tmp_path)
        # Upstream replaces the file: new mtime and new size.
        hub.listing = [_entry("one.jpg", MTIME_B, 5)]
        hub.files = {"one.jpg": b"abcde"}
        result = sync_art(client, BASE_URL, tmp_path)

    assert result == SyncResult(added=0, updated=1, removed=0)
    assert (tmp_path / "one.jpg").read_bytes() == b"abcde"


def test_removes_local_files_missing_upstream(tmp_path):
    stale = tmp_path / "gone.jpg"
    stale.write_bytes(b"old")
    hub = FakeHub(
        listing=[_entry("one.jpg", MTIME_A, 3)],
        files={"one.jpg": b"abc"},
    )
    with httpx.Client(transport=hub.transport) as client:
        result = sync_art(client, BASE_URL, tmp_path)

    assert result == SyncResult(added=1, updated=0, removed=1)
    assert not stale.exists()
    assert (tmp_path / "one.jpg").exists()


def test_ignores_directory_entries(tmp_path):
    hub = FakeHub(
        listing=[
            _entry("one.jpg", MTIME_A, 3),
            _entry("subdir", MTIME_A, 0, type="directory"),
        ],
        files={"one.jpg": b"abc"},
    )
    with httpx.Client(transport=hub.transport) as client:
        result = sync_art(client, BASE_URL, tmp_path)

    assert result == SyncResult(added=1, updated=0, removed=0)
    assert not (tmp_path / "subdir").exists()


def test_unreachable_hub_leaves_dest_untouched(tmp_path):
    kept = tmp_path / "keep.jpg"
    kept.write_bytes(b"keep")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("hub is down")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = sync_art(client, BASE_URL, tmp_path)

    assert result == SyncResult(added=0, updated=0, removed=0)
    assert kept.read_bytes() == b"keep"


def test_listing_error_status_leaves_dest_untouched(tmp_path):
    kept = tmp_path / "keep.jpg"
    kept.write_bytes(b"keep")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = sync_art(client, BASE_URL, tmp_path)

    assert result == SyncResult(added=0, updated=0, removed=0)
    assert kept.read_bytes() == b"keep"


def test_creates_dest_when_missing(tmp_path):
    dest = tmp_path / "art"  # does not exist yet
    hub = FakeHub(
        listing=[_entry("one.jpg", MTIME_A, 3)],
        files={"one.jpg": b"abc"},
    )
    with httpx.Client(transport=hub.transport) as client:
        result = sync_art(client, BASE_URL, dest)

    assert result == SyncResult(added=1, updated=0, removed=0)
    assert (dest / "one.jpg").read_bytes() == b"abc"


def test_rejects_path_traversal_names(tmp_path):
    listing = [
        _entry("ok.jpg", MTIME_A, 3),
        _entry("../evil.jpg", MTIME_A, 3),
        _entry("sub/nested.jpg", MTIME_A, 3),
        _entry("..", MTIME_A, 0),
    ]
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/art/":
            return httpx.Response(200, json=listing)
        requested.append(request.url.path)
        return httpx.Response(200, content=b"abc")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = sync_art(client, BASE_URL, tmp_path / "dest")

    assert result.added == 1
    assert (tmp_path / "dest" / "ok.jpg").exists()
    # Nothing escaped the destination directory.
    assert not (tmp_path / "evil.jpg").exists()
    assert sorted(p.name for p in (tmp_path / "dest").iterdir()) == ["ok.jpg"]
    # Only the safe name was ever requested; the traversing names were rejected.
    assert requested == ["/art/ok.jpg"]


def test_rejects_file_whose_declared_length_exceeds_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(artsync, "_MAX_FILE_BYTES", 4)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/art/":
            return httpx.Response(200, json=[_entry("big.jpg", MTIME_A, 100)])
        return httpx.Response(
            200, content=b"x" * 100, headers={"content-length": "100"}
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = sync_art(client, BASE_URL, tmp_path)

    assert result.added == 0
    assert list(tmp_path.iterdir()) == []


def test_stops_and_discards_a_stream_past_the_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(artsync, "_MAX_FILE_BYTES", 4)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/art/":
            return httpx.Response(200, json=[_entry("big.jpg", MTIME_A, 3)])
        # No content-length header, but the body is larger than the cap.
        return httpx.Response(200, content=b"xxxxxxxx")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = sync_art(client, BASE_URL, tmp_path)

    assert result.added == 0
    # No stray .part file and nothing written.
    assert list(tmp_path.iterdir()) == []
