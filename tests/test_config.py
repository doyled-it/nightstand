from pathlib import Path

from nightstand.config import load_config


def test_local_overrides_default(tmp_path):
    p = tmp_path / "local.toml"
    p.write_text(
        '[location]\nlatitude = 1.5\nlongitude = 2.5\n'
        '[schedule]\nquiet_start = "23:00"\n'
    )
    c = load_config(p)
    assert (c.location.latitude, c.location.longitude) == (1.5, 2.5)
    assert c.schedule.quiet_start == "23:00" and c.schedule.quiet_end == "06:00"
    assert c.game.team_id == 135 and c.art.rotate_minutes == 60


def test_art_min_aspect_default_and_override(tmp_path):
    assert load_config().art.min_aspect == 1.3
    p = tmp_path / "local.toml"
    p.write_text("[art]\nmin_aspect = 1.6\n")
    assert load_config(p).art.min_aspect == 1.6


def test_check_seconds_defaults_to_10(tmp_path):
    # The frame reads now-playing.json every 10 seconds (a small LAN file read),
    # so progress stays current between the 10-minute redraw buckets.
    assert load_config().schedule.check_seconds == 10
    p = tmp_path / "local.toml"
    p.write_text("[schedule]\ncheck_seconds = 30\n")
    assert load_config(p).schedule.check_seconds == 30


def test_missing_location_is_none():
    assert load_config(Path("/nonexistent")).location.latitude is None


def test_defaults_without_path():
    c = load_config()
    assert c.hub.host == "house.local"
    assert c.hub.snapserver_port == 1780
    assert c.schedule.priority == ("morning", "music", "game", "art")
    assert c.paths.cache == Path("/var/lib/nightstand/cache")


def test_default_health_urls():
    c = load_config()
    assert c.health.urls == (
        "http://house.local/status.json",
        "http://office.local/status.json",
        "http://nightstand.local/status.json",
        "http://nightstand.local/nightstand/status.json",
    )


def test_env_var_is_read(tmp_path, monkeypatch):
    p = tmp_path / "env.toml"
    p.write_text('[hub]\nhost = "other.local"\n')
    monkeypatch.setenv("NIGHTSTAND_CONFIG", str(p))
    assert load_config().hub.host == "other.local"


def test_example_config_disables_optional_location_fields():
    # config.example.toml ships 0.0 coordinates and an empty timezone as visible
    # placeholders; load_config normalizes those to None (weather disabled,
    # system local zone).
    example = Path(__file__).resolve().parents[1] / "config.example.toml"
    c = load_config(example)
    assert c.location.latitude is None
    assert c.location.longitude is None
    assert c.location.timezone is None
