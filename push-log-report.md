# Push-log report

Task: add INFO logging to the nightstand display loop so an owner can tell why
the frame stayed on one painting.

## Status: done

- Branch: doyled-it/ns-push-log (fresh commit on the existing worktree branch)
- HEAD: e3397d1c88c3574ba940ea4669824d49282e646d
- PR: https://github.com/doyled-it/nightstand/pull/49
- CI: pass (actions run 42603129246)

## What changed

`src/nightstand/app.py`, `run_once`:

- One INFO line each push: screen, piece id or track title, and forced vs
  scheduled. Helper `_push_detail` picks the identifier (art piece id, falling
  back to title; now-playing/split track title; nothing for other screens), so
  no data beyond what the screens already show reaches the log.
- One INFO line when a changed decision is held: reason is "settling" (identity
  waiting out the settle) or "quiet hours" (should_push declines a changed,
  unforced, non-game decision). Helper `_log_held` plus a new `AppState.last_held`
  field dedupe: logged only when the reason or decision changes, and cleared on
  a push, so a steady hold does not spam every 10 s cycle.

## Tests

TDD with caplog in `tests/test_app.py`: push line content, settling hold logged
once per change, quiet-hours hold logged once, and steady state producing no
held spam. Full suite: 362 passed, 2 skipped. ruff: clean.

## Left

Nothing. Device untouched; no subagents or review skills used.
