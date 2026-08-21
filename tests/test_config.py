import pytest

from tracker.config import ConfigError, load_profiles, load_settings

GOOD = """
[settings]
marketplace = "EBAY_GB"
sweep_minutes = 15
dry_run = true

[[profile]]
id = "a7iii"
name = "Sony A7 III"
query = "sony a7 iii body"
ceiling = 1200
target = 950

  [profile.filters]
  buying_options = ["FIXED_PRICE"]

[[profile]]
id = "rtx"
query = "rtx 4090"
ceiling = 1600
enabled = false
"""

ENV = {
    "EBAY_CLIENT_ID": "id",
    "EBAY_CLIENT_SECRET": "secret",
    "TELEGRAM_BOT_TOKEN": "token",
    "TELEGRAM_CHAT_ID": "chat",
}


@pytest.fixture
def config(tmp_path):
    def write(text=GOOD):
        p = tmp_path / "profiles.toml"
        p.write_text(text, encoding="utf-8")
        return p

    return write


@pytest.fixture
def env(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)


class TestProfiles:
    def test_pounds_convert_to_pence(self, config):
        profiles = load_profiles(config())
        assert profiles[0].ceiling_pence == 120_000
        assert profiles[0].target_pence == 95_000

    def test_defaults_applied(self, config):
        rtx = load_profiles(config())[1]
        assert rtx.name == "rtx"
        assert rtx.target_pence is None
        assert rtx.min_feedback_pct == 95.0
        assert rtx.enabled is False

    def test_filters_preserved(self, config):
        assert load_profiles(config())[0].filters == {
            "buying_options": ["FIXED_PRICE"]
        }

    def test_missing_required_key_is_rejected(self, config):
        with pytest.raises(ConfigError, match="ceiling"):
            load_profiles(config('[[profile]]\nid="x"\nquery="y"\n'))

    def test_duplicate_ids_are_rejected(self, config):
        text = '[[profile]]\nid="x"\nquery="a"\nceiling=10\n' * 2
        with pytest.raises(ConfigError, match="duplicate"):
            load_profiles(config(text))

    def test_target_above_ceiling_is_rejected(self, config):
        """Otherwise the search can never alert, with no obvious reason why."""
        with pytest.raises(ConfigError, match="exceeds"):
            load_profiles(config('[[profile]]\nid="x"\nquery="y"\nceiling=100\ntarget=200\n'))

    def test_empty_config_is_rejected(self, config):
        with pytest.raises(ConfigError, match="no .*profile"):
            load_profiles(config("[settings]\n"))

    def test_missing_file_is_rejected(self, tmp_path):
        with pytest.raises(ConfigError, match="not found"):
            load_profiles(tmp_path / "nope.toml")


class TestSettings:
    def test_reads_toml_and_env(self, config, env):
        s = load_settings(config())
        assert s.sweep_minutes == 15
        assert s.dry_run is True
        assert s.ebay_client_id == "id"

    def test_defaults_when_section_absent(self, config, env):
        s = load_settings(config('[[profile]]\nid="x"\nquery="y"\nceiling=10\n'))
        assert s.sweep_minutes == 30
        assert s.marketplace == "EBAY_GB"

    def test_missing_secrets_are_named(self, config, monkeypatch):
        for k in ENV:
            monkeypatch.delenv(k, raising=False)
        with pytest.raises(ConfigError) as exc:
            load_settings(config())
        for k in ENV:
            assert k in str(exc.value)


class TestDatabasePathOverride:
    def test_env_var_overrides_the_config_file(self, config, env, monkeypatch):
        """Containers point at a mounted volume without a modified config."""
        monkeypatch.setenv("TRACKER_DB", "/data/tracker.db")
        assert load_settings(config()).db_path == "/data/tracker.db"

    def test_config_file_wins_when_env_is_unset(self, config, env, monkeypatch):
        monkeypatch.delenv("TRACKER_DB", raising=False)
        text = GOOD.replace('marketplace = "EBAY_GB"',
                            'marketplace = "EBAY_GB"\ndb_path = "custom.db"')
        assert load_settings(config(text)).db_path == "custom.db"


class TestContainerBinding:
    def test_web_host_env_override(self, config, env, monkeypatch):
        """A container binds 0.0.0.0 so its platform proxy can reach it."""
        monkeypatch.setenv("WEB_HOST", "0.0.0.0")
        assert load_settings(config()).web_host == "0.0.0.0"

    def test_web_port_env_override(self, config, env, monkeypatch):
        monkeypatch.setenv("WEB_PORT", "9999")
        assert load_settings(config()).web_port == 9999

    def test_config_file_wins_when_env_unset(self, config, env, monkeypatch):
        monkeypatch.delenv("WEB_HOST", raising=False)
        assert load_settings(config()).web_host == "127.0.0.1"


class TestPlatformPort:
    def test_railway_style_port_is_used(self, config, env, monkeypatch):
        """Railway and most PaaS hosts inject PORT and expect the app to bind it."""
        monkeypatch.setenv("PORT", "4567")
        assert load_settings(config()).web_port == 4567

    def test_port_beats_web_port(self, config, env, monkeypatch):
        monkeypatch.setenv("PORT", "4567")
        monkeypatch.setenv("WEB_PORT", "9999")
        assert load_settings(config()).web_port == 4567

    def test_falls_back_to_config_file(self, config, env, monkeypatch):
        for v in ("PORT", "WEB_PORT"):
            monkeypatch.delenv(v, raising=False)
        assert load_settings(config()).web_port == 8000
