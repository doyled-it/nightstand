"""Shared test fixtures.

``assert_golden`` compares a rendered image against a stored golden PNG. The
screens snap to the six pure panel colors, so a golden and a fresh render are
discrete-color images; they match exactly on the same machine. Across machines
they can differ by a handful of pixels along glyph edges, because FreeType
anti-aliases text slightly differently on arm64 (a dev Mac) and x86_64 (CI)
before the snap tips an edge pixel to the other side (measured at about 0.27%
of pixels for these screens). So the comparison allows up to ``TOLERANCE`` of
the pixels to differ rather than demanding an exact match, which keeps the
goldens a reliable guard on layout and content while staying reproducible on
any architecture.

Set ``UPDATE_GOLDENS=1`` to write the golden instead of comparing, which is how
goldens are (re)generated after a deliberate visual change. On a failure the
actual render and an amplified diff are written under ``tests/golden/_out/``
(git-ignored) to make the difference easy to inspect.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest
from PIL import Image, ImageChops

GOLDEN_DIR = Path(__file__).parent / "golden"
OUT_DIR = GOLDEN_DIR / "_out"

# At most this fraction of pixels may differ. 800x480 is 384000 pixels, so this
# is about 1920 pixels. Measured arm64-vs-x86_64 glyph-edge noise is about 1000
# pixels (0.27%) for these screens, so this clears it with margin while staying
# far below any real layout or content change, which moves many thousands.
TOLERANCE = 0.005


@pytest.fixture
def assert_golden() -> Callable[[Image.Image, str], None]:
    def _assert_golden(img: Image.Image, name: str) -> None:
        path = GOLDEN_DIR / name
        actual = img.convert("RGB")
        if os.environ.get("UPDATE_GOLDENS") == "1":
            GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
            actual.save(path)
            return
        assert path.exists(), f"golden {name} is missing; run with UPDATE_GOLDENS=1"
        expected = Image.open(path).convert("RGB")
        assert actual.size == expected.size, (
            f"{name}: size {actual.size} != golden {expected.size}"
        )

        diff = ImageChops.difference(actual, expected)
        if diff.getbbox() is None:
            return
        total = actual.width * actual.height
        differing = sum(
            count
            for count, color in diff.getcolors(maxcolors=total)
            if color != (0, 0, 0)
        )
        if differing / total <= TOLERANCE:
            return

        OUT_DIR.mkdir(parents=True, exist_ok=True)
        stem = Path(name).stem
        actual.save(OUT_DIR / f"{stem}.actual.png")
        diff.point(lambda v: min(255, v * 8)).save(OUT_DIR / f"{stem}.diff.png")
        raise AssertionError(
            f"{name} differs from its golden: {differing}/{total} pixels "
            f"({differing / total:.4%}) exceed the {TOLERANCE:.2%} tolerance. "
            f"See {OUT_DIR}/{stem}.actual.png and {stem}.diff.png."
        )

    return _assert_golden
