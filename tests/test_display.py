from __future__ import annotations

import sys
import types

from PIL import Image

from nightstand.screens.common import HEIGHT, WIDTH


def test_inky_display_sets_image_then_shows(monkeypatch):
    # Inject a fake ``inky`` package so the Pi driver can be exercised on a Mac,
    # where the real ``inky`` library is never installed.
    calls: list[tuple] = []

    device = types.SimpleNamespace(
        set_image=lambda img: calls.append(("set_image", img)),
        show=lambda: calls.append(("show",)),
    )
    inky_pkg = types.ModuleType("inky")
    auto_mod = types.ModuleType("inky.auto")
    auto_mod.auto = lambda: device
    monkeypatch.setitem(sys.modules, "inky", inky_pkg)
    monkeypatch.setitem(sys.modules, "inky.auto", auto_mod)

    from nightstand.display import InkyDisplay

    display = InkyDisplay()
    img = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
    display.show(img)

    assert [name for name, *_ in calls] == ["set_image", "show"]
    assert calls[0][1] is img
