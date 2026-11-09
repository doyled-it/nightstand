from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from nightstand.models import ArtPiece
from nightstand.rotation import Rotation
from nightstand.sources.artcontrol import ArtControl

T0 = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


def pieces(n: int) -> list[ArtPiece]:
    return [
        ArtPiece(path=Path(f"/art/p{i}.jpg"), title=f"P{i}", id=f"p{i}.jpg")
        for i in range(n)
    ]


def cmd(seq: int, action: str = "next", piece: str | None = None) -> ArtControl:
    return ArtControl(
        rotate_minutes=60, folders_enabled=(), seq=seq, action=action, piece=piece
    )


def a_rotation(tmp_path: Path) -> Rotation:
    return Rotation(tmp_path / "rotation.json", clock=lambda: T0)


def test_empty_eligible_gives_none(tmp_path):
    rot = a_rotation(tmp_path)
    assert rot.current([], T0, 60) is None


def test_current_is_stable_within_the_interval_then_advances(tmp_path):
    rot = a_rotation(tmp_path)
    elig = pieces(5)

    first = rot.current(elig, T0, 60)
    assert first is not None
    assert rot.current(elig, T0 + timedelta(minutes=59), 60) == first
    advanced = rot.current(elig, T0 + timedelta(minutes=60), 60)
    assert advanced is not None
    assert advanced.id != first.id


def test_interval_is_measured_from_the_last_change(tmp_path):
    rot = a_rotation(tmp_path)
    elig = pieces(5)

    first = rot.current(elig, T0, 60)
    # Advance at the hour, which resets the clock on the new piece.
    second = rot.current(elig, T0 + timedelta(minutes=60), 60)
    assert second.id != first.id
    # 59 minutes after that change, the new piece still holds.
    assert rot.current(elig, T0 + timedelta(minutes=119), 60) == second


def test_startup_existing_command_only_records_its_seq(tmp_path):
    # Review Focus 4: a frame that boots to an existing command does not replay
    # it; it records the seq and keeps showing its piece.
    rot = a_rotation(tmp_path)
    elig = pieces(5)
    first = rot.current(elig, T0, 60)

    assert rot.apply(cmd(9, "next"), elig, T0) is False
    assert rot.current(elig, T0, 60) == first

    # A later command (higher seq) now acts.
    assert rot.apply(cmd(10, "next"), elig, T0) is True
    assert rot.current(elig, T0, 60).id != first.id


def test_next_advances_and_previous_walks_back(tmp_path):
    rot = a_rotation(tmp_path)
    elig = pieces(5)
    first = rot.current(elig, T0, 60)

    assert rot.apply(cmd(1, "next"), elig, T0) is False  # startup adopt
    assert rot.apply(cmd(2, "next"), elig, T0) is True
    second = rot.current(elig, T0, 60)
    assert second.id != first.id

    assert rot.apply(cmd(3, "next"), elig, T0) is True
    third = rot.current(elig, T0, 60)
    assert third.id not in {first.id, second.id}

    # Previous walks back through what was shown: third -> second -> first.
    assert rot.apply(cmd(4, "previous"), elig, T0) is True
    assert rot.current(elig, T0, 60).id == second.id
    assert rot.apply(cmd(5, "previous"), elig, T0) is True
    assert rot.current(elig, T0, 60).id == first.id


def test_previous_with_empty_back_stack_does_nothing(tmp_path):
    rot = a_rotation(tmp_path)
    elig = pieces(5)
    first = rot.current(elig, T0, 60)
    assert rot.apply(cmd(1, "next"), elig, T0) is False  # startup adopt

    assert rot.apply(cmd(2, "previous"), elig, T0) is False
    assert rot.current(elig, T0, 60) == first


def test_show_a_piece_then_resume_after_one_interval(tmp_path):
    rot = a_rotation(tmp_path)
    elig = pieces(5)
    shown = rot.current(elig, T0, 60)
    assert rot.apply(cmd(1, "next"), elig, T0) is False  # startup adopt

    current_id = rot.current(elig, T0, 60).id
    assert current_id == shown.id
    target = next(piece.id for piece in elig if piece.id != current_id)
    assert rot.apply(cmd(2, "show", target), elig, T0) is True
    assert rot.current(elig, T0, 60).id == target
    # Held for a full interval.
    assert rot.current(elig, T0 + timedelta(minutes=30), 60).id == target
    # Then rotation resumes.
    after = rot.current(elig, T0 + timedelta(minutes=60), 60)
    assert after.id != target


def test_show_an_ineligible_piece_is_ignored(tmp_path):
    rot = a_rotation(tmp_path)
    elig = pieces(5)
    rot.current(elig, T0, 60)
    assert rot.apply(cmd(1, "next"), elig, T0) is False  # startup adopt
    before = rot.current(elig, T0, 60)

    assert rot.apply(cmd(2, "show", "Nope/missing.jpg"), elig, T0) is False
    assert rot.current(elig, T0, 60) == before


def test_next_with_a_single_eligible_piece_does_not_change(tmp_path):
    rot = a_rotation(tmp_path)
    elig = pieces(1)
    only = rot.current(elig, T0, 60)
    assert rot.apply(cmd(1, "next"), elig, T0) is False  # startup adopt

    assert rot.apply(cmd(2, "next"), elig, T0) is False
    assert rot.current(elig, T0, 60) == only


def test_restart_resumes_the_current_piece(tmp_path):
    state = tmp_path / "rotation.json"
    rot = Rotation(state, clock=lambda: T0)
    elig = pieces(5)
    rot.current(elig, T0, 60)
    assert rot.apply(cmd(1, "next"), elig, T0) is False
    assert rot.apply(cmd(2, "next"), elig, T0) is True
    current_id = rot.current(elig, T0, 60).id

    # A fresh process reading the same state file resumes, not jumps.
    rot2 = Rotation(state, clock=lambda: T0)
    resumed = rot2.current(elig, T0 + timedelta(minutes=1), 60)
    assert resumed.id == current_id


def test_restart_does_not_replay_a_command_waiting_in_the_file(tmp_path):
    # Review Focus 4: a command issued while the frame was down is adopted as the
    # baseline on the next boot, not acted on; a later command then acts.
    state = tmp_path / "rotation.json"
    rot = Rotation(state, clock=lambda: T0)
    elig = pieces(5)
    rot.current(elig, T0, 60)
    rot.apply(cmd(1, "next"), elig, T0)  # startup adopt
    rot.apply(cmd(2, "next"), elig, T0)  # acts

    rot2 = Rotation(state, clock=lambda: T0)
    before = rot2.current(elig, T0 + timedelta(minutes=1), 60)
    assert rot2.apply(cmd(3, "next"), elig, T0 + timedelta(minutes=1)) is False
    assert rot2.current(elig, T0 + timedelta(minutes=1), 60) == before
    assert rot2.apply(cmd(4, "next"), elig, T0 + timedelta(minutes=1)) is True


def test_no_command_still_consumes_the_startup_grace(tmp_path):
    # A frame that boots with no command (command: null) must act on the first
    # real command it then sees while running.
    rot = a_rotation(tmp_path)
    elig = pieces(5)
    first = rot.current(elig, T0, 60)

    none_cmd = ArtControl(60, (), seq=None, action=None, piece=None)
    assert rot.apply(none_cmd, elig, T0) is False  # consumes the grace
    assert rot.apply(cmd(1, "next"), elig, T0) is True
    assert rot.current(elig, T0, 60).id != first.id


def test_back_stack_is_capped_at_twenty(tmp_path):
    state = tmp_path / "rotation.json"
    rot = Rotation(state, clock=lambda: T0)
    elig = pieces(30)
    rot.current(elig, T0, 60)
    rot.apply(cmd(1, "next"), elig, T0)  # startup adopt
    for seq in range(2, 30):
        rot.apply(cmd(seq, "next"), elig, T0)

    saved = json.loads(state.read_text())
    assert len(saved["back"]) <= 20


def test_ignores_a_corrupt_state_file(tmp_path):
    state = tmp_path / "rotation.json"
    state.write_text("{not json")
    rot = Rotation(state, clock=lambda: T0)
    elig = pieces(3)

    assert rot.current(elig, T0, 60) is not None
