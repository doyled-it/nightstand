from __future__ import annotations

import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

from nightstand.history import ArtHistory
from nightstand.models import ArtPiece

SHOWN = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)


def _piece(name: str, **kwargs) -> ArtPiece:
    return ArtPiece(path=Path("/art") / name, title=name.split(".")[0], **kwargs)


def _read(path: Path) -> dict:
    return json.loads(path.read_text())


def test_record_writes_newest_first_with_current(tmp_path):
    path = tmp_path / "art-history.json"
    history = ArtHistory(path)

    history.record(_piece("a.jpg", artist="Alice"), SHOWN)
    history.record(
        _piece("b.jpg", artist=None, source_url="https://example.org/b"),
        SHOWN + timedelta(hours=1),
    )

    data = _read(path)
    assert [entry["file"] for entry in data["history"]] == ["b.jpg", "a.jpg"]
    assert data["current"] == data["history"][0]
    assert data["current"]["file"] == "b.jpg"


def test_entry_shape(tmp_path):
    path = tmp_path / "art-history.json"
    ArtHistory(path).record(
        _piece("a.jpg", artist="Alice", source_url="https://example.org/a"), SHOWN
    )

    entry = _read(path)["history"][0]
    assert entry == {
        "file": "a.jpg",
        "title": "a",
        "artist": "Alice",
        "source_url": "https://example.org/a",
        "shown_at": SHOWN.isoformat(),
    }
    assert "+00:00" in entry["shown_at"]


def test_null_artist_and_source_url_serialize_as_null(tmp_path):
    path = tmp_path / "art-history.json"
    ArtHistory(path).record(_piece("a.jpg", artist=None, source_url=None), SHOWN)

    entry = _read(path)["history"][0]
    assert entry["artist"] is None
    assert entry["source_url"] is None


def test_file_mode_is_0644(tmp_path):
    path = tmp_path / "art-history.json"
    ArtHistory(path).record(_piece("a.jpg"), SHOWN)

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o644


def test_caps_at_fifty_newest_kept(tmp_path):
    path = tmp_path / "art-history.json"
    history = ArtHistory(path)
    for i in range(60):
        history.record(_piece(f"piece-{i:02d}.jpg"), SHOWN + timedelta(hours=i))

    data = _read(path)
    assert len(data["history"]) == 50
    assert data["history"][0]["file"] == "piece-59.jpg"
    assert data["history"][-1]["file"] == "piece-10.jpg"


def test_same_piece_redraw_does_not_duplicate(tmp_path):
    path = tmp_path / "art-history.json"
    history = ArtHistory(path)

    first = history.record(_piece("a.jpg"), SHOWN)
    second = history.record(_piece("a.jpg"), SHOWN + timedelta(minutes=5))

    assert first is True
    assert second is False
    assert [entry["file"] for entry in _read(path)["history"]] == ["a.jpg"]


def test_same_piece_after_another_is_a_new_showing(tmp_path):
    path = tmp_path / "art-history.json"
    history = ArtHistory(path)

    history.record(_piece("a.jpg"), SHOWN)
    history.record(_piece("b.jpg"), SHOWN + timedelta(hours=1))
    recorded = history.record(_piece("a.jpg"), SHOWN + timedelta(hours=2))

    assert recorded is True
    files = [entry["file"] for entry in _read(path)["history"]]
    assert files == ["a.jpg", "b.jpg", "a.jpg"]


def test_reload_survives_restart(tmp_path):
    path = tmp_path / "art-history.json"
    ArtHistory(path).record(_piece("a.jpg"), SHOWN)

    # A fresh instance (an app restart) must see the existing history and not
    # re-add the piece that is still current.
    reopened = ArtHistory(path)
    added = reopened.record(_piece("a.jpg"), SHOWN + timedelta(minutes=5))

    assert added is False
    assert [entry["file"] for entry in _read(path)["history"]] == ["a.jpg"]


def test_missing_file_starts_fresh(tmp_path):
    path = tmp_path / "art-history.json"
    history = ArtHistory(path)

    added = history.record(_piece("a.jpg"), SHOWN)

    assert added is True
    assert _read(path)["history"][0]["file"] == "a.jpg"


def test_corrupt_file_recovers_with_one_warning(tmp_path, caplog):
    path = tmp_path / "art-history.json"
    path.write_text("{ this is not json")

    with caplog.at_level("WARNING"):
        history = ArtHistory(path)

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1

    history.record(_piece("a.jpg"), SHOWN)
    assert [entry["file"] for entry in _read(path)["history"]] == ["a.jpg"]


def test_history_without_list_recovers(tmp_path):
    path = tmp_path / "art-history.json"
    path.write_text(json.dumps({"current": None, "history": "oops"}))

    history = ArtHistory(path)
    history.record(_piece("a.jpg"), SHOWN)

    assert [entry["file"] for entry in _read(path)["history"]] == ["a.jpg"]
