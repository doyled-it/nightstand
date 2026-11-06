# AGENTS.md

Guidance for coding agents working in `nightstand`. Read this before making
changes.

## What this is

Software for a bedside e-ink picture frame (Pimoroni Inky Impression 7.3 inch,
Spectra 6) driven by a Raspberry Pi. It shows art, now-playing audio, live
Padres games, and the morning weather. The full design is in
`docs/superpowers/specs/2026-11-06-nightstand-design.md`.

## Commands

The project uses [uv](https://docs.astral.sh/uv/). Python 3.11+.

- `uv sync` installs dependencies into `.venv`.
- `uv run pytest` runs the test suite.
- `uv run ruff check` lints. Add `--fix` to apply safe fixes.
- `uv run nightstand` runs the CLI entry point.
- `uv sync --extra pi` adds the Pi-only `inky` driver (installs only on the device).

CI runs `uv sync`, `uv run ruff check`, and `uv run pytest` on every push and
pull request. Keep all three green.

## Layout

```
src/nightstand/
  config.py     load_config() and the frozen Config dataclasses
  palette.py    the six-color palette (PURE, PANEL) and conversions
  text.py       font() loader and wrap() word wrapping
  cli.py        console entry point
  fonts/        bundled Inter TTFs and their license (OFL.txt)
tests/          pytest tests, one module per source module
config.example.toml   every config key with its default and placeholders
```

Local config overlays the defaults. The app reads an explicit path, then
`$NIGHTSTAND_CONFIG`, then `/etc/nightstand/local.toml`. Real coordinates and
anything identifying live in the untracked local config, never in the repo.

## The six-color rule

The panel renders only six colors: black, white, red, yellow, green, blue.
Everything shown must reduce to these. Use `palette.to_six_colors` for rendered
UI (snaps to the nearest pure color, no dithering) and `palette.dither_to_panel`
for photographs and album art (Floyd-Steinberg dither). `palette.simulate_panel`
shows how a design will read on the muted panel when previewing on a Mac.

## Testing

Test-driven development: write the failing test, watch it fail, then implement.
Run the focused test while iterating and the full suite before committing.
Fixtures should be recorded real responses where a data source is involved.

Golden image tests compare a render against a stored PNG via `assert_golden`
(in `tests/conftest.py`). Renders snap to the six pure panel colors, so a
golden and a fresh render match exactly on the same machine. Across machines
they can differ by a few glyph-edge pixels, because FreeType anti-aliases text
differently on arm64 (a dev Mac) and x86_64 (CI) before the snap (measured at
about 0.27% of pixels), so the comparison tolerates up to 0.5% of pixels
differing. Regenerate goldens on any
architecture with `UPDATE_GOLDENS=1 uv run pytest`, then open each PNG and check
it before committing. Failures write the actual render and an amplified diff
under the git-ignored `tests/golden/_out/`.

## No em dashes

Do not use em dashes anywhere: not in code, comments, docs, UI copy, commit
messages, or pull request text. Use a period, a colon, a semicolon, commas, or
parentheses instead.

## This is a multi-repo system

`nightstand` is one repo among several. A feature usually spans repos, and the
seams (the API contract with the hub, config, deploy, and each repo's docs) are
where a change ships half-wired. Before calling a change done, cover its full
cross-repo surface. The repo map, the seams, and the definition of done are in
the hub repo: `snaphouse/docs/cross-repo-features.md`. See also `CONTRIBUTING.md`.
