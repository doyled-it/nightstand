from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from PIL import Image

from nightstand.art import ArtLibrary, art_slot, load_piece_image
from nightstand.models import ArtPiece

NOW = datetime(2026, 11, 6, 12, 0, 0)


def test_art_slot_steps_once_per_rotation():
    rotate = 60
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    base = art_slot(start, rotate)
    assert art_slot(start + timedelta(minutes=59), rotate) == base
    assert art_slot(start + timedelta(minutes=60), rotate) == base + 1


def _write_image(path: Path, color: tuple[int, int, int] = (10, 20, 30)) -> None:
    Image.new("RGB", (64, 64), color).save(path)


def test_caption_from_sidecar(tmp_path):
    _write_image(tmp_path / "coastal.jpg")
    (tmp_path / "coastal.jpg.txt").write_text("Coastal Study\nArtist Name\n")

    piece = ArtLibrary(tmp_path).pieces()[0]

    assert piece.title == "Coastal Study"
    assert piece.artist == "Artist Name"


def test_caption_title_only_sidecar(tmp_path):
    _write_image(tmp_path / "study.png")
    (tmp_path / "study.png.txt").write_text("Just A Title\n")

    piece = ArtLibrary(tmp_path).pieces()[0]

    assert piece.title == "Just A Title"
    assert piece.artist is None


def test_sidecar_source_url_when_https(tmp_path):
    _write_image(tmp_path / "coastal.jpg")
    (tmp_path / "coastal.jpg.txt").write_text(
        "Coastal Study\nArtist Name\nhttps://example.org/art/123\n"
    )

    piece = ArtLibrary(tmp_path).pieces()[0]

    assert piece.title == "Coastal Study"
    assert piece.artist == "Artist Name"
    assert piece.source_url == "https://example.org/art/123"


def test_sidecar_source_url_ignored_when_not_https(tmp_path):
    _write_image(tmp_path / "coastal.jpg")
    (tmp_path / "coastal.jpg.txt").write_text(
        "Coastal Study\nArtist Name\nnot a url\n"
    )

    piece = ArtLibrary(tmp_path).pieces()[0]

    assert piece.source_url is None


def test_sidecar_source_url_rejects_non_https_scheme(tmp_path):
    _write_image(tmp_path / "coastal.jpg")
    (tmp_path / "coastal.jpg.txt").write_text(
        "Coastal Study\nArtist Name\nhttp://example.org/art/123\n"
    )

    piece = ArtLibrary(tmp_path).pieces()[0]

    assert piece.source_url is None


def test_sidecar_source_url_with_empty_artist(tmp_path):
    # Line 2 blank, line 3 a URL: the artist is None and the URL still parses.
    _write_image(tmp_path / "coastal.jpg")
    (tmp_path / "coastal.jpg.txt").write_text(
        "Coastal Study\n\nhttps://example.org/art/123\n"
    )

    piece = ArtLibrary(tmp_path).pieces()[0]

    assert piece.title == "Coastal Study"
    assert piece.artist is None
    assert piece.source_url == "https://example.org/art/123"


def test_two_line_sidecar_has_no_source_url(tmp_path):
    _write_image(tmp_path / "coastal.jpg")
    (tmp_path / "coastal.jpg.txt").write_text("Coastal Study\nArtist Name\n")

    piece = ArtLibrary(tmp_path).pieces()[0]

    assert piece.source_url is None


def test_caption_from_filename(tmp_path):
    _write_image(tmp_path / "coastal_study-1.jpg")

    piece = ArtLibrary(tmp_path).pieces()[0]

    assert piece.title == "coastal study 1"
    assert piece.artist is None


def test_pieces_filtered_and_sorted(tmp_path):
    _write_image(tmp_path / "b.png")
    _write_image(tmp_path / "a.jpg")
    _write_image(tmp_path / "c.webp")
    (tmp_path / "notes.txt").write_text("ignore me")
    (tmp_path / "skip.gif").write_bytes(b"not listed")

    names = [p.path.name for p in ArtLibrary(tmp_path).pieces()]

    assert names == ["a.jpg", "b.png", "c.webp"]


def test_pick_stable_within_slot_and_changes_across_slots(tmp_path):
    for i in range(6):
        _write_image(tmp_path / f"art_{i}.png")
    lib = ArtLibrary(tmp_path)

    first = lib.pick(NOW, 60)
    same_slot = lib.pick(NOW + timedelta(minutes=1), 60)
    next_slot = lib.pick(NOW + timedelta(minutes=60), 60)

    assert first == same_slot
    assert first != next_slot


def test_corrupt_image_skipped(tmp_path):
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"nope")

    assert load_piece_image(ArtPiece(path=bad, title="Bad", artist=None)) is None


def test_load_piece_image_reads_real_file(tmp_path):
    good = tmp_path / "good.png"
    _write_image(good)

    image = load_piece_image(ArtPiece(path=good, title="Good", artist=None))

    assert image is not None
    assert image.size == (64, 64)


def test_empty_library_pick_is_none(tmp_path):
    assert ArtLibrary(tmp_path).pick(NOW, 60) is None


def test_pick_skips_corrupt_piece_for_every_slot(tmp_path):
    # One corrupt file among several good ones. pick must walk the slot's
    # shuffled order past the corrupt file and return a readable piece for
    # every slot of the day, never the broken one.
    for i in range(5):
        _write_image(tmp_path / f"good_{i}.png")
    bad = tmp_path / "zbad.jpg"
    bad.write_bytes(b"nope")
    lib = ArtLibrary(tmp_path)

    for slot in range(48):
        now = NOW + timedelta(minutes=60 * slot)
        piece = lib.pick(now, 60)
        assert piece is not None
        assert piece.path != bad
        assert load_piece_image(piece) is not None


def test_pick_returns_none_when_every_piece_is_corrupt(tmp_path):
    for i in range(3):
        (tmp_path / f"bad_{i}.jpg").write_bytes(b"nope")

    assert ArtLibrary(tmp_path).pick(NOW, 60) is None


def _write_sized(path: Path, size: tuple[int, int]) -> None:
    Image.new("RGB", size, (10, 20, 30)).save(path)


def test_pick_skips_pieces_below_min_aspect(tmp_path):
    # Two wide pieces (3:2) among three tall ones (2:3). With a 1.3 minimum, pick
    # must return a wide piece for every slot and never a tall one.
    wide = set()
    for i in range(2):
        _write_sized(tmp_path / f"wide_{i}.png", (300, 200))
        wide.add(f"wide_{i}.png")
    for i in range(3):
        _write_sized(tmp_path / f"tall_{i}.png", (200, 300))
    lib = ArtLibrary(tmp_path)

    for slot in range(48):
        now = NOW + timedelta(minutes=60 * slot)
        piece = lib.pick(now, 60, 1.3)
        assert piece is not None
        assert piece.path.name in wide


def test_pick_returns_none_when_no_piece_meets_min_aspect(tmp_path):
    for i in range(3):
        _write_sized(tmp_path / f"tall_{i}.png", (200, 300))

    assert ArtLibrary(tmp_path).pick(NOW, 60, 1.3) is None


def test_pieces_include_one_level_folders_with_relative_ids(tmp_path):
    _write_image(tmp_path / "top.jpg")
    (tmp_path / "Photos").mkdir()
    _write_image(tmp_path / "Photos" / "beach.jpg")

    pieces = {p.id: p for p in ArtLibrary(tmp_path).pieces()}

    assert set(pieces) == {"top.jpg", "Photos/beach.jpg"}
    assert pieces["Photos/beach.jpg"].path == tmp_path / "Photos" / "beach.jpg"


def test_pieces_skip_trash_and_hidden_and_deeper_folders(tmp_path):
    _write_image(tmp_path / "keep.jpg")
    (tmp_path / ".trash").mkdir()
    _write_image(tmp_path / ".trash" / "gone.jpg")
    (tmp_path / "Photos").mkdir()
    (tmp_path / "Photos" / "deep").mkdir()
    _write_image(tmp_path / "Photos" / "deep" / "nested.jpg")

    ids = {p.id for p in ArtLibrary(tmp_path).pieces()}

    assert ids == {"keep.jpg"}


def test_folder_sidecar_caption_is_read(tmp_path):
    (tmp_path / "Photos").mkdir()
    _write_image(tmp_path / "Photos" / "beach.jpg")
    (tmp_path / "Photos" / "beach.jpg.txt").write_text("Beach Day\nMe\n")

    piece = next(p for p in ArtLibrary(tmp_path).pieces() if p.id == "Photos/beach.jpg")

    assert piece.title == "Beach Day"
    assert piece.artist == "Me"


def test_eligible_filters_portrait_pieces(tmp_path):
    _write_sized(tmp_path / "wide.png", (300, 200))
    (tmp_path / "Photos").mkdir()
    _write_sized(tmp_path / "Photos" / "wide.png", (300, 200))
    _write_sized(tmp_path / "tall.png", (200, 300))

    ids = {p.id for p in ArtLibrary(tmp_path).eligible(1.3)}

    assert ids == {"wide.png", "Photos/wide.png"}


def test_eligible_empty_library_is_empty(tmp_path):
    assert ArtLibrary(tmp_path).eligible(1.3) == []


def test_pick_caches_aspect_and_does_not_redecode_each_cycle(tmp_path, monkeypatch):
    import nightstand.art as art_module

    for i in range(4):
        _write_sized(tmp_path / f"wide_{i}.png", (300, 200))
    lib = ArtLibrary(tmp_path)

    decodes: list[Path] = []
    real = art_module.load_piece_image

    def counting(piece):
        decodes.append(piece.path)
        return real(piece)

    monkeypatch.setattr(art_module, "load_piece_image", counting)

    for slot in range(48):
        lib.pick(NOW + timedelta(minutes=60 * slot), 60, 1.3)

    # Each file is decoded at most once despite 48 picks: the aspect is cached.
    assert len(decodes) <= 4
