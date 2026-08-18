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
