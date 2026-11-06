# nightstand Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Each task maps to one GitHub issue, one Orca worktree, and one PR.

**Goal:** The bedside e-ink frame app: five screens rendered from live data, chosen by config-driven rules, previewable on a Mac, installed on `pi-bedroom` with self-update and art sync.

**Architecture:** A Python package. Data sources (snapserver, MLB, Open-Meteo, MusicBrainz, the hub's art share, health status files) produce small dataclasses. Pure screen renderers turn them into 800x480 Pillow images. A rules module picks the screen and a redraw key each minute. The app loop pushes to the panel only when the key changes and policy allows. Every renderer can write a PNG on a Mac.

**Tech Stack:** Python 3.11+, uv, Pillow, httpx, Pimoroni `inky` (Pi only, optional extra), pytest + respx, ruff, GitHub Actions, systemd.

**Spec:** `docs/superpowers/specs/2026-11-06-nightstand-design.md`, and for the seams, `snaphouse/docs/cross-repo-features.md`.

## Global Constraints

- Panel: 800x480, six colors. Drawing colors are pure RGB: black `(0,0,0)`, white `(255,255,255)`, red `(255,0,0)`, yellow `(255,255,0)`, green `(0,255,0)`, blue `(0,0,255)`. Every image handed to a display contains only these six.
- Panel approximation colors (for dithering and `--panel` previews): black `#191E21`, white `#E8E8E8`, yellow `#EFDE44`, red `#B21318`, blue `#2157BA`, green `#125F20`.
- Python floor 3.11 (Raspberry Pi OS Bookworm). Dependencies stay small: `pillow`, `httpx`; `inky` only in the `pi` extra.
- No API keys or accounts. No personal or identifying data in the repo or in requests. Latitude, longitude, and timezone come from the untracked local config only. MusicBrainz User-Agent is `nightstand/<version> ( https://github.com/doyled-it/nightstand )`, with no email.
- Outbound hosts allowed: the hub and `pi-bedroom` on the LAN, `statsapi.mlb.com`, `www.mlbstatic.com`, `api.open-meteo.com`, `musicbrainz.org`, `coverartarchive.org` (and its image redirects), `collectionapi.metmuseum.org`, `images.metmuseum.org`, `api.artic.edu`, `www.artic.edu`, GitHub, PyPI.
- No copyrighted images, logos, or art in the repo. Fonts must be freely licensed (Inter, OFL) with the license committed.
- Schedule defaults: quiet hours 22:00 to 06:00; morning window weekdays 06:00 to 08:30, weekends 07:00 to 09:00; art rotates every 60 minutes; game final held 60 minutes; Padres team id 135; check interval 60 s.
- Priority default: morning, music, game, art. Quiet hours block every redraw except the game screen's.
- Game redraws only on: a run scoring, a half-inning ending, the game ending. The game screen shows no count, outs, or runners.
- No em dashes anywhere in prose, comments, or UI text.

## Review Focus

1. Very long titles, artist names, play descriptions: text wraps (title at most 3 lines, play summary at most 2) and ends in an ellipsis; nothing overflows the panel. Test in Tasks 3 and 5.
2. Hub unreachable or snapserver down: the cycle falls through to art, logs once per outage, and retries next minute. Test in Task 8.
3. A game still live at 22:30, a doubleheader, a postponed game: live games keep updating in quiet hours; a doubleheader picks the live game, then the latest final; postponed counts as no game. Test in Tasks 5 and 7.
4. Empty art folder or a corrupt image: corrupt files are skipped, and an empty library renders a built-in placeholder ("Add art to the share") instead of crashing. Test in Task 2.
5. Daylight-saving changes and weekend detection: windows are computed in local time from the configured timezone, so 06:00 is 06:00 on both sides of a DST change. Test in Task 7.

---

## File map

```
pyproject.toml, uv.lock, ruff config in pyproject
config.example.toml                      # documented local config (no real values)
src/nightstand/
  __init__.py, __main__.py
  cli.py            # `nightstand run | preview | selftest | art sync | art fetch | record-fixtures`
  config.py         # Config dataclasses, load_config()
  palette.py        # colors, to_six_colors(), dither_to_panel()
  fonts/            # Inter-Regular.ttf, Inter-Bold.ttf, Inter-Black.ttf, OFL.txt
  text.py           # fit/wrap/ellipsis helpers
  models.py         # NowPlaying, Team, GameState, HourForecast, Forecast, ArtPiece, Health
  art.py            # ArtLibrary, load_piece_image()
  sources/snapserver.py, coverart.py, mlb.py, weather.py, health.py, artsync.py, museums.py
  screens/art.py, now_playing.py, game.py, weather.py, split.py, placeholder.py, marker.py
  rules.py          # Context, Decision, decide(), should_push(), in_quiet_hours(), in_morning_window()
  app.py            # AppState, run_once(), run_forever(), Sources
  display.py        # Display protocol, FileDisplay, InkyDisplay
deploy/install.sh, update.sh, systemd/*.service, systemd/*.timer
tests/fixtures/...  # recorded JSON responses, sample images (generated, not copyrighted)
tests/golden/...    # approved PNGs
.github/workflows/ci.yml
AGENTS.md, CLAUDE.md (shim: `@AGENTS.md`)
```

---

### Task 1: Scaffold, config, palette, text helpers

**Files:**
- Create: `pyproject.toml`, `config.example.toml`, `src/nightstand/{__init__,__main__,cli,config,palette,text}.py`, `src/nightstand/fonts/*`, `.github/workflows/ci.yml`, `AGENTS.md`, `CLAUDE.md`
- Test: `tests/test_config.py`, `tests/test_palette.py`, `tests/test_text.py`

**Interfaces:**
- Produces:
  - `load_config(path: Path | None = None) -> Config`. Reads the package default, then overlays the file at `path`, else `$NIGHTSTAND_CONFIG`, else `/etc/nightstand/local.toml` if present.
  - `Config` (frozen dataclasses): `hub.host="zimablade.local"`, `hub.snapserver_port=1780`, `hub.http_port=80`; `location.latitude: float|None`, `location.longitude: float|None`, `location.timezone: str|None` (None means system local); `schedule.quiet_start="22:00"`, `schedule.quiet_end="06:00"`, `schedule.morning_weekday=("06:00","08:30")`, `schedule.morning_weekend=("07:00","09:00")`, `schedule.priority=("morning","music","game","art")`, `schedule.check_seconds=60`; `art.dir=Path("/var/lib/nightstand/art")`, `art.rotate_minutes=60`; `game.team_id=135`, `game.final_hold_minutes=60`; `paths.cache`, `paths.state`, `paths.public` under `/var/lib/nightstand/`; `health.urls: tuple[str,...]` (default: `http://zimablade.local/status.json`, `http://pi-office.local/status.json`, `http://pi-bedroom.local/status.json`, `http://pi-bedroom.local/nightstand/status.json`).
  - `palette.PURE: dict[str, tuple[int,int,int]]`, `palette.PANEL: dict[str, tuple[int,int,int]]`, `to_six_colors(img) -> Image` (nearest pure color, no dither, used on rendered UI), `dither_to_panel(img) -> Image` (Floyd-Steinberg quantize against `PANEL`, then map indices to `PURE`), `simulate_panel(img) -> Image` (map `PURE` to `PANEL` for previews).
  - `text.font(weight: Literal["regular","bold","black"], size: int) -> ImageFont.FreeTypeFont`, `text.wrap(draw, s, font, max_width, max_lines) -> list[str]` (last line ends in "…" when truncated).

- [ ] **Step 1: Write failing tests**

```python
def test_local_overrides_default(tmp_path):
    p = tmp_path / "local.toml"; p.write_text('[location]\nlatitude = 1.5\nlongitude = 2.5\n[schedule]\nquiet_start = "23:00"\n')
    c = load_config(p)
    assert (c.location.latitude, c.location.longitude) == (1.5, 2.5)
    assert c.schedule.quiet_start == "23:00" and c.schedule.quiet_end == "06:00"
    assert c.game.team_id == 135 and c.art.rotate_minutes == 60

def test_missing_location_is_none():
    assert load_config(Path("/nonexistent")).location.latitude is None

def test_dither_outputs_only_pure_colors():
    img = Image.linear_gradient("L").convert("RGB").resize((800, 480))
    assert set(c for _, c in dither_to_panel(img).getcolors(1 << 20)) <= set(PURE.values())

def test_wrap_truncates_with_ellipsis():
    lines = wrap(draw, "word " * 200, font("bold", 64), 300, 3)
    assert len(lines) == 3 and lines[-1].endswith("…")
```

- [ ] **Step 2: Run `uv run pytest -q`.** Expected: FAIL (modules missing).
- [ ] **Step 3: Implement** the interfaces above. Download Inter static TTFs (Regular, Bold, Black) and `OFL.txt` from the rsms/inter release into `fonts/`. `pyproject` declares `[project.optional-dependencies] pi = ["inky>=2.1"]` (the release line that supports the 7.3" Spectra 6, E673; confirm at install time) and a `nightstand` console script. `config.example.toml` documents every key with placeholder coordinates (`0.0`). CI runs `uv sync`, `uv run ruff check`, `uv run pytest`. AGENTS.md: commands, layout, the six-color rule, the no-em-dash rule, a pointer to `snaphouse/docs/cross-repo-features.md`.
- [ ] **Step 4: Run `uv run pytest -q && uv run ruff check`.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: scaffold package, config, palette, text helpers`.

---

### Task 2: Art library, art screen, placeholder, preview command

**Files:**
- Create: `src/nightstand/models.py` (ArtPiece, Health only for now), `art.py`, `screens/art.py`, `screens/placeholder.py`, `display.py` (FileDisplay), CLI `preview`
- Test: `tests/test_art.py`, `tests/test_screens_art.py`, `tests/golden/art.png`, `tests/golden/placeholder.png`

**Interfaces:**
- Produces:
  - `ArtPiece(path: Path, title: str, artist: str | None)`.
  - `ArtLibrary(dir: Path)`: `.pieces() -> list[ArtPiece]` (extensions `.jpg .jpeg .png .webp`, sorted by name; caption from sidecar `<file>.txt`, where line 1 is the title and line 2, if present, the artist; else title from the filename stem with `_`/`-` turned into spaces). `.pick(now: datetime, rotate_minutes: int) -> ArtPiece | None` is deterministic per slot: slot = epoch minutes // rotate_minutes, and the order is a shuffle seeded by the day so the sequence varies day to day.
  - `load_piece_image(piece) -> Image | None`: None on unreadable file (logged).
  - `screens.art.render(piece: ArtPiece, image: Image) -> Image`: cover-crop to 800x480, `dither_to_panel`, black caption bar 44 px tall at the bottom, 24 px bold white text `"{title} · {artist}"` (or just title).
  - `screens.placeholder.render(message: str) -> Image`.
  - `Display` protocol `show(img: Image) -> None`. `FileDisplay(path: Path, panel: bool = False)`.
  - CLI: `nightstand preview art --art-dir DIR [--at ISO] [--panel] -o out.png`.

- [ ] **Step 1: Write failing tests**

```python
def test_caption_from_sidecar(tmp_path): ...  # "Coastal Study\nArtist Name" -> title, artist
def test_caption_from_filename(tmp_path): ...  # "coastal_study-1.jpg" -> "coastal study 1", None
def test_pick_stable_within_slot_and_changes_across_slots(lib): ...
def test_corrupt_image_skipped(tmp_path):  # a .jpg containing b"nope" -> load_piece_image returns None
def test_empty_library_pick_is_none(tmp_path): assert ArtLibrary(tmp_path).pick(NOW, 60) is None
def test_render_art_matches_golden(): assert_golden(render(piece, sample_image()), "art.png")
def test_placeholder_matches_golden(): assert_golden(placeholder.render("Add art to the share"), "placeholder.png")
```
`assert_golden(img, name)` lives in `tests/conftest.py`: it compares pixels exactly, and writes the file instead when `UPDATE_GOLDENS=1`. The sample image is generated in code (gradients and shapes), not downloaded.

- [ ] **Step 2: Run tests.** Expected: FAIL.
- [ ] **Step 3: Implement.** Generate goldens with `UPDATE_GOLDENS=1`, then open each PNG and check it against the approved mockup (round three) before committing.
- [ ] **Step 4: Run tests.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: art library, art screen, preview`.

---

### Task 2b: Panel bring-up on the prototype Pi (owner present)

The owner has a Raspberry Pi with the Inky panel attached to the Mac mini, so real-panel checks start here instead of waiting for the frame.

**Files:**
- Modify: `display.py` (InkyDisplay), `cli.py` (`show`)
- Create: `docs/prototype.md` (how to reach the prototype Pi from the Mac and push an image; hostnames only, no addresses)

**Interfaces:**
- Produces: `nightstand show PATH [--saturation N]`: loads a PNG and pushes it to the panel through `InkyDisplay`. The Mac loop: `nightstand preview <screen> -o x.png && scp x.png <pi>:/tmp/ && ssh <pi> nightstand show /tmp/x.png`.

- [ ] **Step 1:** Get SSH access to the prototype Pi (network path agreed with the owner). Expected: `ssh <pi> uname -m` prints `aarch64`.
- [ ] **Step 2:** On the Pi: enable SPI and I2C, add `dtoverlay=spi0-0cs`, install uv, clone the repo, `uv sync --extra pi`. Run Pimoroni's own example to confirm the panel and the `inky.auto()` detection (expected: it reports the 7.3" Spectra 6 / E673).
- [ ] **Step 3:** Implement `show` and push `tests/golden/art.png` and `placeholder.png`. Expected: both appear in about 30 s, colors as in the preview.
- [ ] **Step 4:** Photograph the panel next to the `--panel` preview of the same image. If the `PANEL` approximation colors (Global Constraints) are visibly off, adjust them in `palette.py`, regenerate goldens, and note the change in the PR.
- [ ] **Step 5: Commit** `feat: show command and panel bring-up notes`.

---

### Task 3: Now playing from snapserver

**Files:**
- Create: `sources/snapserver.py`, `screens/now_playing.py`; extend `models.py`
- Test: `tests/test_snapserver.py`, `tests/test_screens_now_playing.py`, fixtures `tests/fixtures/snapserver/{playing_airplay,playing_bluetooth,idle,all_muted}.json`

**Interfaces:**
- Consumes: `text.wrap`, `palette`.
- Produces:
  - `NowPlaying(title: str, artist: str, album: str | None, art_url: str | None, rooms: tuple[str, ...])`.
  - `fetch_status(client: httpx.Client, host: str, port: int) -> dict`: POST `{"id":1,"jsonrpc":"2.0","method":"Server.GetStatus"}` to `http://{host}:{port}/jsonrpc` with a 5 s timeout; returns `result`.
  - `parse_now_playing(status: dict) -> NowPlaying | None`. Use the first stream whose `status == "playing"` that at least one unmuted group is assigned to. Metadata comes from `stream.properties.metadata`: `title`, `artist` (a list, joined with ", "), `album`, `artUrl`. Rooms are the clients in groups whose `stream_id` is that stream, where the group is unmuted, the client is `connected`, and `config.volume.muted` is false, named `config.name or host.name`, in group order. No rooms, or a missing title, means None.
  - `screens.now_playing.render(np: NowPlaying, cover: Image | None) -> Image`. With a cover: 480x480 dithered cover on the left; text column from x=506 with title 64 px black weight (max 3 lines), artist 38 px bold (max 2), album 28 px regular (max 1), room chips (22 px bold, white on black, 6 px gaps) bottom-aligned at y=446. Without a cover, the text column starts at x=40.
- Fixtures are hand-written to snapserver's documented `Server.GetStatus` shape. Task 5 of the snaphouse plan replaces them with recordings from the real hub (tracked in the cross-repo doc).

- [ ] **Step 1: Failing tests:** `parse` returns the right title, joined artists, and rooms `("Living room","Bedroom")` for `playing_airplay`; `idle` gives None; `all_muted` gives None; a 300-character title renders without pixels beyond x=794 in the text column (check the bounding box of non-white pixels); golden PNGs for the with-cover and without-cover layouts.
- [ ] **Step 2: Run tests.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run tests.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: now playing screen from snapserver`.

---

### Task 4: Cover art (AirPlay URL or MusicBrainz lookup), cached

**Files:**
- Create: `sources/coverart.py`
- Test: `tests/test_coverart.py` (respx-mocked)

**Interfaces:**
- Produces: `CoverArt(client: httpx.Client, cache_dir: Path)` with `.for_track(np: NowPlaying) -> Image | None`:
  1. If `np.art_url`, GET it and cache under `sha1(url)`.
  2. Else if `np.album`, MusicBrainz `GET https://musicbrainz.org/ws/2/release/?query=artist:"{artist}" AND release:"{album}"&fmt=json&limit=1`, then `GET https://coverartarchive.org/release/{mbid}/front-500` (follow redirects). Cache the image under `sha1(artist|album)`, and cache a miss as a `.none` marker for 7 days.
  3. Never more than one MusicBrainz request per second (simple monotonic-time gate). User-Agent per Global Constraints.

- [ ] **Step 1: Failing tests:** a hit fetches once, and a second call reads the cache (respx call count 1); a miss writes the `.none` marker and makes no request the second time; `art_url` skips MusicBrainz entirely; the User-Agent header matches exactly and contains no `@`.
- [ ] **Step 2: Run tests.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run tests.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: cover art lookup with cache`.

---

### Task 5: Padres game from the MLB Stats API

**Files:**
- Create: `sources/mlb.py`, `screens/game.py`, `scripts/record_mlb_fixtures.py` (or CLI `record-fixtures mlb`)
- Test: `tests/test_mlb.py`, `tests/test_screens_game.py`, fixtures `tests/fixtures/mlb/{schedule_doubleheader,schedule_postponed,feed_scoring_play,feed_middle_inning,feed_end_inning,feed_final,feed_preview}.json`

**Interfaces:**
- Produces:
  - `Team(id: int, abbrev: str, runs: int)`; `GameState(game_pk: int, status: Literal["live","final"], away: Team, home: Team, inning: int, inning_state: Literal["Top","Middle","Bottom","End"], summary: str, ended_at: datetime | None)`.
  - `find_game(client, team_id: int, day: date) -> int | None`: `GET https://statsapi.mlb.com/api/v1/schedule?sportId=1&teamId={team_id}&date={YYYY-MM-DD}`. Prefer a game whose `status.abstractGameState == "Live"`, else the latest `Final`; postponed or suspended (`detailedState` contains "Postponed" or "Suspended") counts as no game.
  - `fetch_feed(client, game_pk) -> dict`: `GET https://statsapi.mlb.com/api/v1.1/game/{pk}/feed/live`.
  - `parse_game(feed: dict) -> GameState | None` (None for Preview). `ended_at` comes from `gameData.datetime` or the last play's `about.endTime` when final.
  - `game_redraw_key(g: GameState) -> tuple` = `(g.game_pk, g.away.runs, g.home.runs, completed_halves(g), g.status)` where `completed_halves = 2*(inning-1) + {"Top":0,"Middle":1,"Bottom":1,"End":2}[inning_state]`. Middle and Bottom of the same inning share a key, so the frame redraws once per half, not twice.
  - `summarize(feed, g) -> str`:
    - final: `"Final: {winner} win {w}-{l}"`.
    - `inning_state in ("Middle","End")`: n = runs in the half just completed (`linescore.innings[inning-1].{away|home}.runs`, missing means 0). n>0 gives `"{abbrev} scored {n} in the {top|bottom} {ordinal(inning)}"`; n=0 gives `"No runs in the {top|bottom} {ordinal(inning)}"`.
    - otherwise: the description of `allPlays[scoringPlays[-1]]` if any scoring play happened in the current half, else `""`.
  - `state_label(g) -> str`: `"Top 7th"`, `"Middle 7th"`, `"Bottom 7th"`, `"End 7th"`, `"Final"`.
  - `screens.game.render(g: GameState, away_logo: Image | None, home_logo: Image | None) -> Image`: state label 44 px black-weight red at top-left; logos 150x150 (fallback: abbreviation 64 px bold); runs 200 px black weight; 4 px rule; summary 30 px (max 2 lines) at the bottom. Logos come from `https://www.mlbstatic.com/team-logos/{id}.svg`, rasterized to PNG once and cached in `paths.cache/logos/` (use `cairosvg` only if Pillow can't load SVG; if adding it, note it in AGENTS.md; prefer the PNG variant MLB serves if one exists).
- Fixture recorder: given a past `gamePk` and timecodes, saves `feed/live?timecode=YYYYMMDD_HHMMSS` snapshots. Use a 2026 regular-season Padres game. The recorder lives in the repo; the recorded JSON is public MLB data and may be committed.

- [ ] **Step 1: Failing tests:** each fixture parses to the expected state and summary string; the doubleheader schedule picks the Live game; postponed gives None; `feed_preview` gives None; `game_redraw_key` is equal for the Middle and Bottom of the same inning and differs after a run; `ordinal` covers 1st, 2nd, 3rd, 4th, 11th, 12th, 13th, 21st; a 400-character summary stays within 2 lines; golden PNG.
- [ ] **Step 2: Run tests.** Expected: FAIL.
- [ ] **Step 3: Implement**, recording fixtures first.
- [ ] **Step 4: Run tests.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: Padres game screen`.

---

### Task 6: Weather and split screens

**Files:**
- Create: `sources/weather.py`, `screens/weather.py`, `screens/split.py`
- Test: `tests/test_weather.py`, `tests/test_screens_weather.py`, fixture `tests/fixtures/openmeteo/forecast.json` (recorded with placeholder coordinates `0,0` replaced by any public city center; the coordinates are stripped from the saved JSON)

**Interfaces:**
- Produces:
  - `HourForecast(time: datetime, temp: int, rain_pct: int, uv: int)`; `Forecast(now_temp: int, description: str, high: int, low: int, hours: tuple[HourForecast, ...], sunrise: datetime, sunset: datetime, uv_peak: int, uv_peak_time: datetime)`.
  - `fetch_forecast(client, lat, lon, tz) -> dict`: `GET https://api.open-meteo.com/v1/forecast` with `latitude`, `longitude`, `current=temperature_2m,weather_code`, `hourly=temperature_2m,precipitation_probability,uv_index`, `daily=temperature_2m_max,temperature_2m_min,sunrise,sunset`, `temperature_unit=fahrenheit`, `timezone={tz or "auto"}`, `forecast_days=1`.
  - `parse_forecast(data, now) -> Forecast`: `hours` holds 5 entries starting at the next whole hour, every 2 hours; temperatures rounded; UV rounded.
  - `describe(code: int) -> str`: WMO weather code to a short phrase ("Clear", "Partly cloudy", "Fog", "Drizzle", "Rain", "Showers", "Thunderstorms", and so on).
  - `what_to_wear(f: Forecast) -> str`: buckets by temperature: `<55` "Warm layers", `55-64` "Jacket", `65-74` "T-shirt", `>=75` "Shorts". If `bucket(now_temp) != bucket(high)`, `"{A} now, {B} later"`, else `"{A} weather"`. Append `" + umbrella"` if any hour's `rain_pct >= 50`.
  - `screens.weather.render(f, now) -> Image`: per round-three mockup (temperature 170 px, description 36 px, high/low 28 px, wear line 30 px bold on yellow, table with TEMP/RAIN/UV rows, UV>=6 white on red, sunrise/sunset bar filled by daylight fraction).
  - `screens.split.render(f, np: NowPlaying, cover: Image | None, now) -> Image`: weather without the UV row (UV peak moved into the high/low line as `"UV {n} at {h}{am|pm}"` on red when >=6) plus an 84 px black strip: a 60x60 cover (or yellow square), title 32 px bold, artist 28 px, rooms 22 px right-aligned.

- [ ] **Step 1: Failing tests:** parse gives 5 hours at 2-hour steps from the next hour; `what_to_wear` cases: (61, high 72) "Jacket now, T-shirt later"; (68, 72) "T-shirt weather"; (50, 52, rain 60) "Warm layers weather + umbrella"; golden PNGs for weather and split.
- [ ] **Step 2: Run tests.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run tests.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: weather and split screens`.

---

### Task 7: Rules engine

**Files:**
- Create: `src/nightstand/rules.py`
- Test: `tests/test_rules.py`

**Interfaces:**
- Consumes: the models and `game_redraw_key` (Task 5).
- Produces:
  - `Context(now_playing: NowPlaying | None, game: GameState | None, forecast: Forecast | None, art: ArtPiece | None)`.
  - `Decision(screen: Literal["art","now_playing","game","weather","split","placeholder"], redraw_key: tuple)`.
  - `in_quiet_hours(now: datetime, cfg) -> bool` (handles windows crossing midnight); `in_morning_window(now, cfg) -> bool` (weekday = Monday to Friday by `now.weekday()`, in the configured or system timezone).
  - `decide(now, cfg, ctx) -> Decision`: walk `cfg.schedule.priority`:
    - `morning`: if in window and `ctx.forecast`, then `split` if `ctx.now_playing` else `weather`;
    - `music`: if `ctx.now_playing`;
    - `game`: if `ctx.game` and (status live, or final and `now - ended_at <= final_hold`);
    - `art`: if `ctx.art`.
    Nothing matched gives `placeholder`.
  - Redraw keys: art `("art", str(path), slot)`; now_playing `("np", title, artist, album, rooms)`; game `("game",) + game_redraw_key(g)`; weather `("wx", date, hour, minute // 30)`; split the weather key plus the np key; placeholder `("placeholder",)`.
  - `should_push(decision, last_key: tuple | None, now, cfg) -> bool`: False if `decision.redraw_key == last_key`; False if in quiet hours and `decision.screen != "game"`; else True.

- [ ] **Step 1: Failing tests** with a fixed `ZoneInfo("America/Los_Angeles")` in the test config only:
  - weekday 06:00 is morning and 05:59 is not; Saturday 06:30 is not, Saturday 07:00 is;
  - DST start (2027-03-14) and end (2026-11-01) mornings both match at 06:00 local;
  - 22:00 is quiet, 05:59 is quiet, 06:00 is not;
  - music outranks game by default, and swapping the priority order flips it;
  - a live game at 22:30 pushes, while art at 22:30 doesn't;
  - a final game shows at +59 min and falls to art at +61 min;
  - an empty context gives placeholder;
  - an unchanged key doesn't push.
- [ ] **Step 2: Run tests.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run tests.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: rules engine`.

---

### Task 8: App loop, health marker, Inky display, status file

**Files:**
- Create: `app.py`, `sources/health.py`, `screens/marker.py`; extend `display.py` (InkyDisplay), `cli.py` (`run`, `selftest`)
- Test: `tests/test_app.py`, `tests/test_health.py`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `Sources` protocol: `now_playing() -> NowPlaying | None`, `cover(np) -> Image | None`, `game() -> GameState | None`, `logo(team_id) -> Image | None`, `forecast() -> Forecast | None`, `art(now) -> ArtPiece | None`, `health() -> Health`. `LiveSources(cfg, client)` implements it. Each call is wrapped so an exception logs once per outage (state change only) and returns None.
  - `Health(problems: tuple[str, ...], stale_since: datetime | None)`. `fetch_health(client, urls) -> tuple[str, ...]`: each URL returns the status schema in the cross-repo doc; a problem is `verify != "pass"`, or `commit != main_commit`, or unreachable for over 15 minutes. `stale_since` is the first time, in the current outage, that a source the decided screen needed returned None.
  - `screens.marker.apply(img, health) -> Image`: with problems or `stale_since` set, draws a 14x14 red square 8 px from the top-right corner, plus, when `stale_since` is set, 16 px text `"since HH:MM"` beside it. The marker state `(bool(problems), stale_since)` is appended to the redraw key so it changes only when the state changes.
  - `AppState(last_key: tuple | None, outage_started: dict[str, datetime])`; `run_once(state, cfg, sources, display, now) -> bool` (True if pushed); `run_forever(cfg)` sleeps `check_seconds` between runs.
  - After each push, write `paths.public/status.json` with the cross-repo schema, filling `verify` from the last `selftest` result recorded by `update.sh`.
  - `InkyDisplay`: `from inky.auto import auto`; `show(img)` calls `set_image(img)` then `show()`. The image is already six-color, so the library does no extra mapping.
  - `nightstand selftest`: renders every screen from the bundled fixtures to a temp dir, exits non-zero on any exception. This is the on-device check `update.sh` runs.

- [ ] **Step 1: Failing tests** with a fake `Sources` and a recording display:
  - an unchanged world pushes once across three cycles;
  - a now-playing change pushes;
  - a snapserver exception falls through to art, logs exactly one warning across five failing cycles, and sets `stale_since`;
  - recovery clears the marker and pushes once;
  - a health problem adds the marker and pushes once;
  - `selftest` exits 0.
- [ ] **Step 2: Run tests.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run tests.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: app loop, health marker, Inky display`.

---

### Task 9: Install, self-update, art sync on the Pi

**Files:**
- Create: `sources/artsync.py`, `deploy/install.sh`, `deploy/update.sh`, `deploy/systemd/nightstand.service`, `nightstand-update.{service,timer}`, `nightstand-art-sync.{service,timer}`; add a shellcheck step to CI
- Test: `tests/test_artsync.py`, `tests/test_update_script.py`

**Interfaces:**
- Consumes: `nightstand selftest`; the hub's `http://{hub}/art/` nginx JSON autoindex (cross-repo doc: entries `{"name","type","mtime","size"}`).
- Produces:
  - `sync_art(client, base_url: str, dest: Path) -> SyncResult(added: int, updated: int, removed: int)`: mirrors the listing (one level, files only), downloads new files or those whose size or mtime changed to a temp name then renames, and deletes local files missing upstream. An unreachable hub leaves `dest` untouched. CLI: `nightstand art sync`.
  - `install.sh` (idempotent, run as root by the snaphouse bootstrap):
    - enable SPI and I2C (`raspi-config nonint do_spi 0`, `do_i2c 0`) and add `dtoverlay=spi0-0cs` to `/boot/firmware/config.txt` if absent (Pimoroni's Pi 5 requirement for Inky Impression; reboot flag if changed);
    - create the system user `nightstand` in groups `spi`, `i2c`, `gpio`;
    - install uv; clone or update `/opt/nightstand`; `uv sync --extra pi --frozen`;
    - copy `config.example.toml` to `/etc/nightstand/local.toml` if missing;
    - create `/var/lib/nightstand/{art,cache,state,public}`;
    - install and enable the units.
  - Timers: update every 15 min with `RandomizedDelaySec=120`; art sync every 10 min.
  - `update.sh`: `git fetch origin main`. If `origin/main` equals HEAD, or equals the recorded failed commit, exit. Otherwise check it out, run `uv sync --extra pi --frozen` and `nightstand selftest`; on success record `last_good` and restart the service; on failure record `failed=<commit>`, check out `last_good`, `uv sync`, restart. Either way write `status.json` (`commit`, `main_commit`, `last_good`, `verify`, `checked_at`, `host`, `repo="nightstand"`).

- [ ] **Step 1: Failing tests:** `sync_art` adds, updates, and removes against respx-mocked listings, and an unreachable hub changes nothing; `test_update_script.py` runs `update.sh` against a temp bare repo with a PATH shim for `uv`, `nightstand`, and `systemctl`, and asserts: a new good commit becomes `last_good`; a commit whose `selftest` fails gets rolled back and is not retried until `main` moves; `status.json` has every schema field.
- [ ] **Step 2: Run tests.** Expected: FAIL.
- [ ] **Step 3: Implement.** `shellcheck deploy/*.sh` clean.
- [ ] **Step 4: Run tests.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: Pi install, self-update, art sync`.
- [ ] **Step 6 (owner present, after frame assembly):** On `pi-bedroom`: the snaphouse bootstrap runs `install.sh`. Run `sudo -u nightstand /opt/nightstand/.venv/bin/nightstand preview art -o /tmp/t.png`, then the Pimoroni test image, then `systemctl status nightstand`. Expected: the panel shows art within 2 minutes of service start. Reboot; expected: same with no manual step.

---

### Task 10: Museum art helper

**Files:**
- Create: `sources/museums.py`, CLI `art fetch`
- Test: `tests/test_museums.py` (respx)

**Interfaces:**
- Produces: `nightstand art fetch {met|aic} --query TEXT [--limit 20] --dest DIR`. Only public-domain items with an image are written: Met `GET https://collectionapi.metmuseum.org/public/collection/v1/search?hasImages=true&q=...`, then `objects/{id}` (keep when `isPublicDomain` and `primaryImage`); AIC `GET https://api.artic.edu/api/v1/artworks/search?q=...&fields=id,title,artist_title,image_id,is_public_domain&limit=...` (keep when `is_public_domain` and `image_id`; image at `https://www.artic.edu/iiif/2/{image_id}/full/843,/0/default.jpg`). Each item is written as `<source>-<id>.jpg` plus a sidecar `<file>.txt` (title, artist). Existing files are skipped.

- [ ] **Step 1: Failing tests:** non-public-domain results are skipped; sidecar contents are correct; a rerun downloads nothing new.
- [ ] **Step 2: Run tests.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run tests.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: museum art helper`.

---

## Order and dependencies

1 → 2 → 2b → (3, 5, 6 in parallel) → 4 (after 3) → 7 → 8 → 9 → 10. Every screen task after 2b ends with a push to the prototype panel. Task 9 step 6 runs first on the prototype Pi (by running `install.sh` by hand), and again on `pi-bedroom` after the snaphouse bootstrap.
