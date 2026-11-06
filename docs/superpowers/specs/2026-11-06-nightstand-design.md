# nightstand: design

Status: draft for owner review, 2026-11-06.
Companion repo: `snaphouse` (whole-house audio). The seams between the two are in
`snaphouse/docs/cross-repo-features.md`.

## What this is and why

A bedside picture frame that mostly shows art and changes when there's something worth
glancing at: what's playing on the house audio, a live Padres game, or the morning
weather while getting dressed. It should look like a framed print, not a gadget, and be
readable from bed.

The hardware is a Pimoroni Inky Impression 7.3" (2025, Spectra 6), with 800x480 pixels in
six colors (black, white, red, yellow, green, blue). It sits in an IKEA RÖDALM 8x10" frame
mounted landscape, driven by `pi-bedroom`, a Raspberry Pi 5 (1 GB) attached to the back
of the panel. The same Pi plays house audio to the bedroom JBL. That part belongs to
`snaphouse`.

The panel is not backlit. It reflects room light like paper, so it's invisible in the dark
and silent when it redraws. A full refresh takes roughly 15 to 30 seconds and visibly
flashes, which shapes most of the timing rules below.

The repo is public on GitHub (`doyled-it/nightstand`). It updates itself on the Pi the
same way `snaphouse` does.

## Screens

Mockups (round three, approved as a starting point) were reviewed with the owner on
2026-11-06. Final tuning happens on the real panel. All screens favor few, large elements:
the panel is about the size of a paperback.

1. **Art.** A full-bleed image dithered to the six colors, with a black caption bar along
   the bottom: title and artist. Rotates every hour.
2. **Now playing.** Album art on the left (480x480) when available, otherwise text uses
   the full width. Title (largest), artist, album, and a row of chips naming the rooms
   currently playing.
3. **Padres game.** Both team logos and run totals (very large), the game state as of the
   last redraw ("Middle 7th", "Top 3rd", "Final"), and one summary line along the bottom:
   either the scoring play that just happened ("Machado 2-run double, SD takes the lead")
   or what the half-inning that just ended produced ("SD scored 2 in the bottom 6th"). No
   count, outs, or runners: with redraws only on big moments they would always be stale.
4. **Morning weather.** Current temperature (very large), a short description, high and
   low, a plain "what to wear" line, then a table of five hourly steps with temperature,
   labeled rain chance, and UV (UV of 6 or more highlighted red), then a sunrise-to-sunset
   progress bar.
5. **Split: weather + now playing.** The weather screen with UV folded into the header
   line, the hourly table trimmed to temperature and rain, and an 84 px black strip along
   the bottom with a small cover square, title, artist, and rooms playing.

## When each screen shows

The app checks every 60 seconds and decides what should be on screen. It redraws only if
the result differs from what's showing and the screen's own redraw rule allows it.

In priority order:

1. **Quiet hours, 22:00 to 06:00.** No redraws, except that a Padres game in progress
   keeps updating under its own rule.
2. **Morning window: weekdays 06:00 to 08:30, weekends 07:00 to 09:00.** Weather. If
   house audio is playing, the split screen instead.
3. **Music playing.** Now playing. It redraws when the track changes.
4. **Padres game in progress.** Game screen. It redraws only when a run scores, a half
   ends, or the game ends. The final score stays up for one hour after the game.
5. **Otherwise art,** rotating hourly.

Music ranks above a live game because the handoff ordered it that way. That's my call
carried forward. The order and all the times live in a config file, so changing them is a
one-line edit.

## Goals

1. The five screens above, rendered from live data, on the real panel.
2. The scheduling rules above, driven by a config file, so adding a screen later (Habitica
   to-dos, today's calendar) means writing one module and adding one rule.
3. A preview mode on a Mac: render any screen to an 800x480 PNG from recorded or live data,
   with no Pi or panel attached. This is the main way layouts get built and tuned.
4. An art pipeline: a local collection synced from the `snaphouse` hub, dithered for the
   panel, plus a tool to help gather images (see Art).
5. Runs as a systemd service, survives reboots and network blips, and updates itself from
   `main`.

## Non-goals (this version)

- Habitica, Google Calendar, Home Assistant, standings, a next-game countdown, quote of the
  day. These are wanted later. The screen/rule design must make them cheap to add, but
  none ship now.
- The Inky's four side buttons. The frame may cover them.
- Pre-game screens. On game days the game screen first appears at first pitch.
- Any interactive control. The frame only displays.

## Anti-goals

- Copyrighted artwork, team logos, or other third-party images committed to the public
  repo. Logos are fetched from MLB's CDN and cached on the device. Art lives on the hub.
- Personal or identifying information in the repo or in outbound requests. Weather is
  queried with city-level coordinates for San Diego, never the home's location. No API
  keys or accounts are needed for anything in this version.
- Refreshing so often that the frame is a constant flicker in a lit room.
- A display that freezes silently on stale content. If a data source fails, the screen
  falls through to the next rule (usually art), and a small marker in a corner shows when
  the frame last updated successfully.

## Data sources

| Screen | Source | Notes |
|---|---|---|
| Now playing | snapserver JSON-RPC on the hub (`Server.GetStatus`) | Stream playback state, metadata (title, artist, album, art URL when the sender provided one), and which clients are unmuted. This is the main seam with `snaphouse`. |
| Now playing art for Bluetooth sources | Lookup by artist + album, or text-only | Bluetooth (the owner's Android) sends no art. See open questions. |
| Padres game | MLB Stats API: `statsapi.mlb.com/api/v1/schedule` to find today's game, `/api/v1.1/game/{gamePk}/feed/live` for state and plays | Public, no key. The same upstream the owner's `mlb-model` and `mlb-races` repos use; those repos' own datasets refresh daily, too stale for this. |
| Team logos | `www.mlbstatic.com/team-logos/{teamId}.svg` | Fetched on first use and cached on the device, never committed. Reduced to the six colors at render time. |
| Weather | Open-Meteo forecast API | No key or account. City-level coordinates in config. Needs hourly temperature, precipitation probability, UV index, and daily sunrise and sunset. |
| Art | Folder synced from the hub's art share | See Art. |

## Art

- Originals live on the `snaphouse` hub, in a shared folder that the owner and partner can
  drop files into from a Mac or phone. `pi-bedroom` syncs it to a local cache on a timer.
  The mechanism is defined in the cross-repo doc.
- Dithering happens on the Pi at display time (or cached per image), using the Inky
  library's Spectra 6 palette handling. Originals are never modified.
- Caption text comes from a small sidecar file next to each image or, failing that, from
  the filename. The format is the builder's call, but it must be hand-editable.
- A helper tool in this repo gathers public-domain works from museum open-access APIs (the
  Met and the Art Institute of Chicago both flag public-domain items) into the hub folder
  with captions filled in. For artists still under copyright, the owner adds images
  personally. The tool never commits images to the repo.
- Building out the owner's collection, including the list of artists they like, is a
  separate task after this ships.

## How it's built (decided)

- **Language:** Python, because Pimoroni's `inky` library is Python. Environment and
  dependencies managed with `uv`. Rendering with Pillow.
- **Structure:** each screen is a module with two jobs: report whether it has something to
  show right now (with whatever data it needs), and draw itself at 800x480. A rules file
  sets priority, schedules, and per-screen redraw policies. A small core loop evaluates
  the rules every 60 seconds, renders, compares against the last image pushed, and pushes
  only when it differs and policy allows.
- **Testing:** pytest. Fixtures are recorded real responses: a snapserver status with music
  playing, an MLB live feed mid-game with a fresh scoring play, a feed between half
  innings, a final, an Open-Meteo forecast. Render tests compare against approved golden
  PNGs. The scheduling rules are tested with a fake clock across the quiet-hours and
  morning-window edges.
- **Preview:** a CLI command renders a named screen from a fixture or live data to a PNG
  on the Mac.
- **Deploy:** a systemd service runs the app. A systemd timer pulls `main` (default every
  15 minutes), syncs dependencies, and restarts the service if anything changed. CI
  (GitHub Actions: lint plus tests) must pass before merge. `main` is protected.
- **First install on `pi-bedroom`:** the `snaphouse` bootstrap for `pi-bedroom` clones this
  repo and runs its installer once. After that, this repo owns everything display-specific
  on the Pi: enabling SPI and I2C, the `inky` library, its service, its updater, and its
  art sync. One bootstrap per device, with clear ownership afterward. That's my call.

## Hardware assembly (from the handoff)

Mount the Pi to the Inky with its standoffs. Cut the RÖDALM mat to the panel's active area
(about 160x97 mm). Mount the frame sideways (landscape), since the board is 174x123 mm and
the frame is portrait. Seat behind the glass. Run the right-angle USB-C cable out the back,
and leave the backing slightly open if it bulges; the assembly is 25 to 30 mm deep and the
frame is 30 mm. Then install Pimoroni's drivers, enable SPI and I2C, and push a test image
before any app work runs on the panel.

## Build order

1. Scaffold: package layout, `uv`, CI, preview command, fixture recorder.
2. Art screen and dithering, previewed on the Mac.
3. Now-playing screen against a recorded snapserver status. It needs a real one from the
   hub once `snaphouse` step 3 is done.
4. Padres game screen and its redraw rule against recorded MLB feeds. Recording real
   in-progress feeds isn't possible until the 2027 season (it's the off-season), so the
   first fixtures come from archived games.
5. Weather and split screens.
6. Rules engine, quiet hours, morning window, fall-through on failure.
7. Frame assembly and panel test (owner present).
8. Installer, service, self-update timer, art sync. Reboot and confirm it comes back.
9. Museum art helper.

## Left to the builder

- Package layout, module names, config file format.
- Fonts (they must be freely licensed and committed, or installed from OS packages).
- Exact pixel layouts, within the approved mockups.
- How the "what to wear" line is derived, as long as it is plain rules with no
  third-party service.
- Image caching and dithering cache strategy.

## Open questions for the owner

- Album art for Bluetooth sources: look it up from an open music database (MusicBrainz
  with the Cover Art Archive, no account) using artist and album, or stay text-only for
  the Android source? A lookup tells that service what's being played, though with nothing
  that identifies the household.
