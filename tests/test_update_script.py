"""Drive deploy/update.sh against a temporary git repo with shimmed commands.

The script shells out to ``git`` (real), and to ``uv``, ``nightstand`` and
``systemctl`` (shimmed onto PATH here). ``nightstand selftest`` passes or fails
per checked-out commit by reading a ``SELFTEST`` file committed at that commit,
so a "bad" commit can be made to fail verify exactly as a broken release would
on the device.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "update.sh"


def _run(cmd: list[str], cwd: Path, env: dict | None = None) -> None:
    subprocess.run(cmd, cwd=cwd, env=env, check=True, capture_output=True, text=True)


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def _commit(repo: Path, selftest: str, message: str) -> str:
    (repo / "SELFTEST").write_text(selftest)
    # A per-commit marker so every commit changes the tree even when the
    # SELFTEST verdict is unchanged from the previous commit.
    (repo / "MARKER").write_text(message)
    _git(repo, "add", "SELFTEST", "MARKER")
    _git(repo, "commit", "--quiet", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _write_shim(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _make_shims(bin_dir: Path, log: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    # uv does nothing but succeed.
    _write_shim(bin_dir / "uv", 'echo "uv $*" >> "$SHIM_LOG"\nexit 0\n')
    # systemctl does nothing but succeed, recording the call.
    _write_shim(bin_dir / "systemctl", 'echo "systemctl $*" >> "$SHIM_LOG"\nexit 0\n')
    # selftest passes unless the checked-out SELFTEST file says "fail".
    _write_shim(
        bin_dir / "nightstand",
        'echo "nightstand $* [$(git rev-parse HEAD)]" >> "$SHIM_LOG"\n'
        'if [ "$1" = selftest ] && [ "$(cat SELFTEST 2>/dev/null)" = fail ]; then\n'
        "  exit 1\n"
        "fi\n"
        "exit 0\n",
    )
    os.environ.setdefault("SHIM_LOG", str(log))


def _env(bin_dir: Path, repo: Path, state: Path, status: Path, log: Path) -> dict:
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["NIGHTSTAND_REPO"] = str(repo)
    env["NIGHTSTAND_STATE"] = str(state)
    env["NIGHTSTAND_STATUS"] = str(status)
    env["NIGHTSTAND_SERVICE"] = "nightstand"
    env["NIGHTSTAND_HOST"] = "pi-bedroom"
    env["SHIM_LOG"] = str(log)
    return env


def _origin(tmp_path: Path) -> tuple[Path, str]:
    """A normal (non-bare) git repo acting as origin, with one good commit A."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "--quiet", "--initial-branch", "main")
    _git(origin, "config", "user.email", "ci@example.invalid")
    _git(origin, "config", "user.name", "CI")
    a = _commit(origin, "pass", "A")
    return origin, a


def _clone(origin: Path, dest: Path) -> Path:
    _git(dest.parent, "clone", "--quiet", str(origin), dest.name)
    _git(dest, "config", "user.email", "ci@example.invalid")
    _git(dest, "config", "user.name", "CI")
    return dest


def _advance(origin: Path, selftest: str, message: str) -> str:
    return _commit(origin, selftest, message)


def _run_update(env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True
    )


def _paths(tmp_path: Path):
    return (
        tmp_path / "bin",
        tmp_path / "state" / "update.state",
        tmp_path / "public" / "status.json",
        tmp_path / "shim.log",
    )


def test_new_good_commit_becomes_last_good(tmp_path):
    origin, _a = _origin(tmp_path)
    repo = _clone(origin, tmp_path / "repo")
    bin_dir, state, status, log = _paths(tmp_path)
    _make_shims(bin_dir, log)
    env = _env(bin_dir, repo, state, status, log)

    b = _advance(origin, "pass", "B")
    result = _run_update(env)

    assert result.returncode == 0, result.stderr
    assert _git(repo, "rev-parse", "HEAD") == b
    assert state.read_text().splitlines()[0] == b
    doc = json.loads(status.read_text())
    assert doc["verify"] == "pass"
    assert doc["commit"] == b
    assert doc["last_good"] == b


def test_status_has_every_schema_field(tmp_path):
    origin, a = _origin(tmp_path)
    repo = _clone(origin, tmp_path / "repo")
    bin_dir, state, status, log = _paths(tmp_path)
    _make_shims(bin_dir, log)
    env = _env(bin_dir, repo, state, status, log)

    _run_update(env)

    doc = json.loads(status.read_text())
    assert set(doc) == {
        "host",
        "repo",
        "commit",
        "main_commit",
        "last_good",
        "verify",
        "checked_at",
    }
    assert doc["repo"] == "nightstand"
    assert doc["host"] == "pi-bedroom"
    assert doc["commit"] == a
    assert doc["main_commit"] == a


def test_bad_commit_rolls_back_and_is_not_retried_until_main_moves(tmp_path):
    origin, _a = _origin(tmp_path)
    repo = _clone(origin, tmp_path / "repo")
    bin_dir, state, status, log = _paths(tmp_path)
    _make_shims(bin_dir, log)
    env = _env(bin_dir, repo, state, status, log)

    # Establish a known-good last_good at B.
    b = _advance(origin, "pass", "B")
    _run_update(env)
    assert _git(repo, "rev-parse", "HEAD") == b

    # A bad commit C: its selftest fails, so it rolls back to B.
    c = _advance(origin, "fail", "C")
    result = _run_update(env)
    assert result.returncode == 0, result.stderr
    assert _git(repo, "rev-parse", "HEAD") == b
    assert state.read_text().splitlines()[1] == c
    doc = json.loads(status.read_text())
    assert doc["verify"] == "pass"  # the rolled-back B verifies
    assert doc["commit"] == b
    assert doc["main_commit"] == c
    assert doc["last_good"] == b

    # Running again with main still at C does not re-check-out or retry C.
    log.write_text("")  # clear the shim log
    result = _run_update(env)
    assert result.returncode == 0, result.stderr
    assert _git(repo, "rev-parse", "HEAD") == b
    assert f"[{c}]" not in log.read_text()

    # Once main moves past the bad commit to a good D, updates resume.
    d = _advance(origin, "pass", "D")
    _run_update(env)
    assert _git(repo, "rev-parse", "HEAD") == d
    assert state.read_text().splitlines()[0] == d


def test_unreachable_origin_changes_nothing_but_writes_status(tmp_path):
    origin, a = _origin(tmp_path)
    repo = _clone(origin, tmp_path / "repo")
    bin_dir, state, status, log = _paths(tmp_path)
    _make_shims(bin_dir, log)
    env = _env(bin_dir, repo, state, status, log)

    # Break the origin so the fetch fails.
    _git(repo, "remote", "set-url", "origin", str(tmp_path / "does-not-exist"))

    result = _run_update(env)

    assert result.returncode == 0, result.stderr
    assert _git(repo, "rev-parse", "HEAD") == a
    doc = json.loads(status.read_text())
    assert doc["commit"] == a
    assert doc["verify"] == "pass"


def test_status_write_does_not_follow_a_planted_symlink(tmp_path):
    origin, a = _origin(tmp_path)
    repo = _clone(origin, tmp_path / "repo")
    bin_dir, state, status, log = _paths(tmp_path)
    _make_shims(bin_dir, log)
    env = _env(bin_dir, repo, state, status, log)

    # An attacker who can write the (unprivileged) public dir plants symlinks at
    # the status path and its once-predictable temp name, aimed at a root file.
    canary = tmp_path / "canary"
    canary.write_text("SECRET")
    status.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(canary, status)
    os.symlink(canary, status.with_name(status.name + ".tmp"))

    result = _run_update(env)

    assert result.returncode == 0, result.stderr
    # The root-written status never clobbered the symlink target.
    assert canary.read_text() == "SECRET"
    doc = json.loads(status.read_text())
    assert doc["commit"] == a


def test_invalid_commit_ids_in_state_are_ignored(tmp_path):
    origin, a = _origin(tmp_path)
    repo = _clone(origin, tmp_path / "repo")
    bin_dir, state, status, log = _paths(tmp_path)
    _make_shims(bin_dir, log)
    env = _env(bin_dir, repo, state, status, log)

    # A tampered state file with non-sha values must not be trusted.
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("not-a-sha\n$(touch pwned)\n")

    result = _run_update(env)

    assert result.returncode == 0, result.stderr
    assert not (repo / "pwned").exists()
    doc = json.loads(status.read_text())
    assert doc["last_good"] == ""
    assert doc["commit"] == a


def test_status_file_is_world_readable_for_nginx(tmp_path):
    origin, _a = _origin(tmp_path)
    repo = _clone(origin, tmp_path / "repo")
    bin_dir, state, status, log = _paths(tmp_path)
    _make_shims(bin_dir, log)
    env = _env(bin_dir, repo, state, status, log)

    _run_update(env)

    mode = status.stat().st_mode
    # nginx (a different user) must be able to read the published status file,
    # so the atomic write cannot leave it at mktemp's private 0600.
    assert mode & stat.S_IROTH
    assert mode & stat.S_IRGRP
