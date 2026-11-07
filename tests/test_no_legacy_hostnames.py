"""Guard against the old machine hostnames creeping back in.

The machines were renamed on 2026-11-06 (zimablade->house, pi-office->office,
pi-bedroom->nightstand). The historical plan under docs/superpowers/plans keeps
the original names on purpose, so it is excluded.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_LEGACY_NAMES = ("zimablade", "pi-office", "pi-bedroom")


def test_no_legacy_hostnames_in_repo():
    for name in _LEGACY_NAMES:
        result = subprocess.run(
            [
                "git",
                "grep",
                "--line-number",
                "--fixed-strings",
                name,
                "--",
                ".",
                ":(exclude)docs/superpowers/plans",
                # This guard file necessarily spells the legacy names out.
                ":(exclude)tests/test_no_legacy_hostnames.py",
            ],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
        )
        # git grep exits 1 when there are no matches, which is what we want.
        assert result.returncode == 1, (
            f"legacy hostname {name!r} still present:\n{result.stdout}"
        )
