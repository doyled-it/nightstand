from __future__ import annotations

import json
import math

import pytest

from nightstand.crops import CropMap, load_drawn, parse_box, save_drawn

GOOD = {"x": 0.1, "y": 0.05, "w": 0.6, "h": 0.6}


def test_parse_box_returns_a_tuple_for_a_good_box():
    assert parse_box(GOOD) == (0.1, 0.05, 0.6, 0.6)


def test_parse_box_accepts_the_whole_picture_and_integers():
    assert parse_box({"x": 0, "y": 0, "w": 1, "h": 1}) == (0.0, 0.0, 1.0, 1.0)


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        "box",
        {},
        {"x": 0, "y": 0, "w": 1},
        {"x": 0, "y": 0, "w": 1, "h": 1, "z": 1},
        {"x": True, "y": 0, "w": 1, "h": 1},
        {"x": "0", "y": 0, "w": 1, "h": 1},
        {"x": None, "y": 0, "w": 1, "h": 1},
        {"x": math.nan, "y": 0, "w": 0.5, "h": 0.5},
        {"x": 0, "y": math.inf, "w": 0.5, "h": 0.5},
        {"x": -0.1, "y": 0, "w": 0.5, "h": 0.5},
        {"x": 0, "y": 0, "w": 0, "h": 0.5},
        {"x": 0, "y": 0, "w": 0.5, "h": 0},
        {"x": 0, "y": 0, "w": 1.2, "h": 0.5},
        {"x": 0.6, "y": 0, "w": 0.5, "h": 0.5},
        {"x": 0, "y": 0.6, "w": 0.5, "h": 0.5},
    ],
)
def test_parse_box_rejects_bad_boxes(bad):
    assert parse_box(bad) is None


def test_crop_map_round_trips_and_ignores_bad_boxes(tmp_path):
    path = tmp_path / "art-crops.json"
    cmap = CropMap(path)
    cmap.save({"a.jpg": (GOOD, None), "P/b.jpg": (None, GOOD)})
    loaded = CropMap(path).load()
    assert loaded["a.jpg"] == ((0.1, 0.05, 0.6, 0.6), None)
    assert loaded["P/b.jpg"] == (None, (0.1, 0.05, 0.6, 0.6))

    path.write_text(
        json.dumps({"pieces": {"a.jpg": {"crop": {"x": 9}, "auto_crop": GOOD}}})
    )
    assert CropMap(path).load() == {"a.jpg": (None, (0.1, 0.05, 0.6, 0.6))}


@pytest.mark.parametrize(
    "text", ["", "not json", "[]", '{"pieces": 3}', '{"pieces": {"a": 4}}']
)
def test_crop_map_malformed_file_is_empty(tmp_path, text):
    path = tmp_path / "art-crops.json"
    path.write_text(text)
    assert CropMap(path).load() == {}


def test_crop_map_missing_file_is_empty(tmp_path):
    assert CropMap(tmp_path / "none.json").load() == {}


def test_crop_map_sees_a_rewrite(tmp_path):
    path = tmp_path / "art-crops.json"
    cmap = CropMap(path)
    cmap.save({"a.jpg": (GOOD, None)})
    assert cmap.load()["a.jpg"][0] is not None
    cmap.save({"a.jpg": (None, None)})
    assert cmap.load()["a.jpg"][0] is None


def test_drawn_record_round_trips(tmp_path):
    path = tmp_path / "art-drawn.json"
    assert load_drawn(path) is None
    save_drawn(path, "P/a.jpg", (0.1, 0.05, 0.6, 0.6))
    assert load_drawn(path) == ("P/a.jpg", (0.1, 0.05, 0.6, 0.6))
    save_drawn(path, "P/a.jpg", None)
    assert load_drawn(path) == ("P/a.jpg", None)


def test_drawn_record_malformed_is_none(tmp_path):
    path = tmp_path / "art-drawn.json"
    path.write_text("{")
    assert load_drawn(path) is None
