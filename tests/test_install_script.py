"""Drive the package-bootstrap step of deploy/install.sh with shimmed commands.

The minimal Raspberry Pi OS (Trixie) image ships without git, which install.sh
needs to clone the repo, so the installer must apt-install any missing base tool
before it is used. The script guards its ``main`` call so it can be sourced and
its functions exercised here without running the real (root-only) install.

``apt-get`` and the probed commands are shimmed onto a restricted PATH: a tool
absent from that PATH reads as missing, exactly as git does on a fresh Pi.
"""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "install.sh"
BASH = shutil.which("bash") or "/bin/bash"


def _write_shim(path: Path, body: str) -> None:
    # An absolute /bin/sh shebang so the shim runs even on the restricted PATH
    # the tests use to make a tool read as missing.
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run_ensure(bin_dir: Path, log: Path) -> subprocess.CompletedProcess:
    # A restricted PATH so only the shims in bin_dir count as present; bash
    # itself is still located via the parent's real PATH by subprocess.
    env = {"PATH": str(bin_dir), "SHIM_LOG": str(log)}
    return subprocess.run(
        [BASH, "-c", f'source "{SCRIPT}"; ensure_base_packages'],
        env=env,
        capture_output=True,
        text=True,
    )


def test_installs_git_when_missing(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "shim.log"
    # Only apt-get exists on PATH, so git and curl both read as missing.
    _write_shim(bin_dir / "apt-get", f'echo "apt-get $*" >> "{log}"\nexit 0\n')

    result = _run_ensure(bin_dir, log)

    assert result.returncode == 0, result.stderr
    logged = log.read_text()
    assert "apt-get update" in logged
    install_line = next(
        line for line in logged.splitlines() if line.startswith("apt-get install")
    )
    assert "git" in install_line


def test_sync_repo_uses_managed_python(tmp_path):
    # inky pulls spidev, which compiles against Python.h. The system Python on a
    # minimal image has no dev headers, so uv must use its own managed Python
    # (whose standalone build ships them) rather than /usr/bin/python3.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "shim.log"
    _write_shim(bin_dir / "git", "exit 0\n")
    _write_shim(
        bin_dir / "uv",
        f'echo "uv $* [py=$UV_PYTHON_INSTALL_DIR]" >> "{log}"\nexit 0\n',
    )

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)

    env = {"PATH": str(bin_dir), "SHIM_LOG": str(log)}
    result = subprocess.run(
        [BASH, "-c", f'source "{SCRIPT}"; REPO_DIR="{repo}"; sync_repo'],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    uv_line = next(
        line for line in log.read_text().splitlines() if line.startswith("uv sync")
    )
    assert "--python-preference only-managed" in uv_line
    assert "--extra pi" in uv_line
    assert "--frozen" in uv_line
    # The managed Python must land in a shared location the service user can
    # reach, not root's private uv directory.
    assert "[py=/opt/uv/python]" in uv_line


def test_no_apt_when_tools_present(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "shim.log"
    _write_shim(bin_dir / "apt-get", f'echo "apt-get $*" >> "{log}"\nexit 0\n')
    _write_shim(bin_dir / "git", "exit 0\n")
    _write_shim(bin_dir / "curl", "exit 0\n")

    result = _run_ensure(bin_dir, log)

    assert result.returncode == 0, result.stderr
    assert not log.exists() or log.read_text() == ""
