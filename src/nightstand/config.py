"""Configuration: frozen defaults overlaid with an optional local TOML file.

``load_config`` starts from the built-in package defaults, then overlays one
file: the explicit ``path`` if given, otherwise ``$NIGHTSTAND_CONFIG``,
otherwise ``/etc/nightstand/local.toml`` when it exists. A path that does not
exist leaves the defaults untouched, so the app always has a complete Config.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

_LOCAL_CONFIG = Path("/etc/nightstand/local.toml")
_STATE_ROOT = Path("/var/lib/nightstand")


@dataclass(frozen=True)
class Hub:
    host: str = "house.local"
    snapserver_port: int = 1780
    http_port: int = 80


@dataclass(frozen=True)
class Location:
    # None latitude/longitude means weather is disabled until the local config
    # supplies city-level coordinates. None timezone means the system local zone.
    latitude: float | None = None
    longitude: float | None = None
    timezone: str | None = None


@dataclass(frozen=True)
class Schedule:
    quiet_start: str = "22:00"
    quiet_end: str = "06:00"
    morning_weekday: tuple[str, str] = ("06:00", "08:30")
    morning_weekend: tuple[str, str] = ("07:00", "09:00")
    priority: tuple[str, ...] = ("morning", "music", "game", "art")
    check_seconds: int = 60


@dataclass(frozen=True)
class Art:
    dir: Path = _STATE_ROOT / "art"
    rotate_minutes: int = 60


@dataclass(frozen=True)
class Game:
    team_id: int = 135
    final_hold_minutes: int = 60


@dataclass(frozen=True)
class Paths:
    cache: Path = _STATE_ROOT / "cache"
    state: Path = _STATE_ROOT / "state"
    public: Path = _STATE_ROOT / "public"


@dataclass(frozen=True)
class HealthConfig:
    # Named ``HealthConfig`` (not ``Health``) so it never collides with
    # ``models.Health``, the runtime health state the corner marker draws.
    urls: tuple[str, ...] = (
        "http://house.local/status.json",
        "http://office.local/status.json",
        "http://nightstand.local/status.json",
        "http://nightstand.local/nightstand/status.json",
    )


@dataclass(frozen=True)
class Config:
    hub: Hub = field(default_factory=Hub)
    location: Location = field(default_factory=Location)
    schedule: Schedule = field(default_factory=Schedule)
    art: Art = field(default_factory=Art)
    game: Game = field(default_factory=Game)
    paths: Paths = field(default_factory=Paths)
    health: HealthConfig = field(default_factory=HealthConfig)


def load_config(path: Path | None = None) -> Config:
    """Return a Config: package defaults overlaid with a local TOML file."""
    config = Config()
    source = _resolve_source(path)
    if source is not None and source.exists():
        config = _overlay(config, tomllib.loads(source.read_text()))
    return _normalize(config)


def _normalize(config: Config) -> Config:
    """Treat placeholder location values as 'unset'.

    config.example.toml ships visible placeholders rather than empty fields, so
    translate them to the sentinels the app expects: 0.0/0.0 coordinates mean
    weather is disabled, an empty timezone means the system local zone.
    """
    location = config.location
    changes = {}
    if location.latitude == 0.0 and location.longitude == 0.0:
        changes["latitude"] = None
        changes["longitude"] = None
    if location.timezone == "":
        changes["timezone"] = None
    if changes:
        return replace(config, location=replace(location, **changes))
    return config


def _resolve_source(path: Path | None) -> Path | None:
    if path is not None:
        return path
    env = os.environ.get("NIGHTSTAND_CONFIG")
    if env:
        return Path(env)
    if _LOCAL_CONFIG.exists():
        return _LOCAL_CONFIG
    return None


def _overlay(config: Config, data: dict) -> Config:
    section_updates = {}
    for section in fields(config):
        values = data.get(section.name)
        if not isinstance(values, dict):
            continue
        current = getattr(config, section.name)
        keys = {f.name for f in fields(current)}
        changes = {
            key: _coerce(getattr(current, key), value)
            for key, value in values.items()
            if key in keys
        }
        if changes:
            section_updates[section.name] = replace(current, **changes)
    return replace(config, **section_updates)


def _coerce(current, value):
    """Shape a raw TOML value to match the field's existing type."""
    if isinstance(current, Path):
        return Path(value)
    if isinstance(current, tuple):
        return tuple(value)
    return value
