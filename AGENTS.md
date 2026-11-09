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
  screens/      one module per screen (art, now_playing, game, ...)
  sources/      one module per data source (snapserver, coverart, mlb, ...)
  fonts/        bundled Inter TTFs and their license (OFL.txt)
scripts/        developer tools (record_mlb_fixtures.py records game fixtures)
tests/          pytest tests, one module per source module
config.example.toml   every config key with its default and placeholders
```

Recorded fixtures are real upstream responses. The MLB game fixtures under
`tests/fixtures/mlb/` are trimmed to the keys the parser reads (see
`scripts/record_mlb_fixtures.py`), since the full feed is hundreds of KB.

Local config overlays the defaults. The app reads an explicit path, then
`$NIGHTSTAND_CONFIG`, then `/etc/nightstand/local.toml`. Real coordinates and
anything identifying live in the untracked local config, never in the repo.

## Published files

The app and the self-updater publish into `paths.public`
(`/var/lib/nightstand/public`), which snaphouse's nginx on the nightstand host
serves over the LAN. Never commit these; they are written on the device.

- `status.json`: this machine's health, written only by the root self-updater
  (`deploy/update.sh`). See the health marker and the cross-repo status schema.
- `art-history.json`: the last 50 art pieces shown, newest first, rewritten
  atomically (0644) by the app each time the art screen is pushed to the panel.
  Shape `{"current": Piece | null, "history": [Piece, ...]}`, `current` the same
  object as `history[0]`, each `Piece` being
  `{"file", "title", "artist", "source_url", "shown_at"}`. snaphouse consumes
  it at `http://nightstand.local/nightstand/art-history.json`; the contract is
  pinned in the hub's `docs/cross-repo-features.md`.

Art sidecar captions (`<image file>.txt`, one beside each image): line 1 title,
line 2 artist (may be empty), optional line 3 the museum object page as an
`https` URL (ignored unless it parses as https). Without a sidecar the title
falls back to the filename.

Art shape: the display rotates only pieces at least `art.min_aspect` times wider
than tall (default 1.3); narrower pieces stay in the share but are skipped.
`ArtLibrary.pick` caches each image's aspect so the filter does not decode every
image each cycle. The art screen cover-crops with a detail-aware window
(`screens/common.detail_crop`) that slides the crop along the overflowing axis
toward the busiest region, so a subject near an edge is not cut. The
`nightstand art fetch` helper discards a downloaded work below `--min-aspect`
(same 1.3 default) before writing it, counting it as "skipped (shape)".

## External hosts (allowlist)

nightstand reaches only these hosts, all keyless public APIs or the LAN hub.
Keep this list current when a data source is added, and send nothing
identifying in any request (no name, email, address, or home location).

- `house.local` (the hub): the now-playing file (`now-playing.json`), the cover
  images it points to (same-origin `/cover/...` paths), the art share, and
  snapserver JSON-RPC, all on the LAN. The hub resolves all artwork now, so the
  frame does no album-cover lookup of its own (MusicBrainz and the Cover Art
  Archive left this list).
- `statsapi.mlb.com`: the MLB Stats API, for the schedule and the game feed.
- `midfield.mlbstatic.com`: team logos. MLB serves a ready-made 150x150 PNG spot
  logo at `/v1/team/{id}/spots/150`, so no SVG rasterizer (cairosvg) is needed.
  Logos are fetched on first use and cached under `paths.cache/logos/`, never
  committed.
- `api.open-meteo.com`: the Open-Meteo forecast API, for the morning weather. No
  key or account. Only city-level coordinates from the untracked local config go
  in the request, never the household's real location. The recorded fixture has
  its latitude, longitude, elevation, and timezone fields stripped.
- The LAN status endpoints in `health.urls` (the hub and each Pi, e.g.
  `http://<host>.local/status.json`): polled for the system-health marker. Each
  serves the shared machine-status schema (see the hub's
  `docs/cross-repo-features.md`). GET only, nothing identifying in the request.
- `collectionapi.metmuseum.org`, `images.metmuseum.org`, `api.artic.edu`,
  `www.artic.edu`: the museum open-access APIs the `nightstand art fetch` helper
  reads to gather public-domain works into the art share. The Met search and
  object lookups use `collectionapi.metmuseum.org` and its images download from
  `images.metmuseum.org`; the Art Institute search uses `api.artic.edu` and its
  IIIF images download from `www.artic.edu`. Reached only by that hand-run
  helper, never by the display loop. Keyless and public; the only request data
  is the owner's search text, nothing identifying.

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
