"""Shared test fixtures.

``assert_golden`` compares a rendered image against a stored golden PNG, pixel
for pixel. Set ``UPDATE_GOLDENS=1`` to write the golden instead of comparing,
which is how goldens are (re)generated after a deliberate visual change.

The goldens are Linux-rendered, matching CI and the Raspberry Pi (the real
render target). Text anti-aliasing is FreeType-build-specific, so regenerate
them on Linux, not macOS, or the text pixels will not match. For example:

    docker run --rm --platform linux/amd64 -v "$PWD":/repo -w /repo \\
      -e UV_PROJECT_ENVIRONMENT=/tmp/venv \\
      ghcr.io/astral-sh/uv:python3.13-bookworm-slim \\
      bash -c "uv sync && UPDATE_GOLDENS=1 uv run pytest tests/test_screens_art.py"
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest
from PIL import Image, ImageChops

GOLDEN_DIR = Path(__file__).parent / "golden"


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
        assert diff.getbbox() is None, f"{name} differs from its golden"

    return _assert_golden
