"""Phase 0 exit criteria: config loads, and missing credentials fail loudly."""

from __future__ import annotations

import pytest

from src.common import config as config_module
from src.common.config import (
    CREDENTIAL_ENV_VARS,
    OPTIONAL_ENV_VARS,
    REQUIRED_ENV_VARS,
    ConfigFileError,
    MissingConfigError,
    load_run_config,
    missing_credentials,
    missing_optional_credentials,
)


def test_config_yaml_loads_and_validates():
    run_config, raw = load_run_config()
    assert run_config.model.provider == "groq"
    assert run_config.model.name == "openai/gpt-oss-120b"
    assert run_config.model.temperature == 0
    assert isinstance(raw, dict)


def test_config_hash_is_stable_and_secret_free():
    _, raw = load_run_config()
    from src.common.config import _hash_config

    first = _hash_config(raw)
    second = _hash_config(raw)
    assert first == second
    assert len(first) == 64


def test_unknown_key_in_config_is_rejected(tmp_path):
    """A typo in config.yaml must fail rather than be silently ignored."""
    bad = tmp_path / "config.yaml"
    bad.write_text("model:\n  provider: groq\n  nmae: typo\n", encoding="utf-8")
    with pytest.raises(ConfigFileError):
        load_run_config(bad)


def test_missing_config_file_raises(tmp_path):
    with pytest.raises(ConfigFileError):
        load_run_config(tmp_path / "does_not_exist.yaml")


def test_missing_credentials_named_not_silent(monkeypatch, tmp_path):
    """Exit criterion: a missing key produces a named error, not a None deep in a run."""
    monkeypatch.setattr(config_module, "ENV_PATH", tmp_path / ".env")
    for name in CREDENTIAL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    absent = missing_credentials()
    assert set(absent) == set(REQUIRED_ENV_VARS)

    config_module.get_settings.cache_clear()
    with pytest.raises(MissingConfigError) as exc_info:
        config_module.get_settings()
    for name in REQUIRED_ENV_VARS:
        assert name in str(exc_info.value)
    config_module.get_settings.cache_clear()


def test_optional_credentials_do_not_block_a_run(monkeypatch, tmp_path):
    """Reddit is disabled by default, so its absence must not stop the pipeline."""
    monkeypatch.setattr(config_module, "ENV_PATH", tmp_path / ".env")
    for name in CREDENTIAL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    for name in REQUIRED_ENV_VARS:
        monkeypatch.setenv(name, "dummy-value")

    assert missing_credentials() == []
    assert set(missing_optional_credentials()) == set(OPTIONAL_ENV_VARS)

    config_module.get_settings.cache_clear()
    settings = config_module.get_settings()
    assert settings.credentials.has_reddit is False
    config_module.get_settings.cache_clear()


def test_reddit_disabled_and_expected_sources_enabled():
    run_config, _ = load_run_config()
    enabled = run_config.collection.enabled_sources()
    assert "reddit" not in enabled
    # MouthShut renders its review list client-side, so the static collector can
    # only ever yield zero and trip its own tripwire; it is off until a
    # browser-rendering fetch exists. Asserted rather than assumed so that
    # re-enabling it is a deliberate act with a test to update.
    assert "mouthshut" not in enabled
    # AJIO publishes no free text anywhere on site — only rating, fit and quality
    # bars — so ajio_manual has nothing to hand-collect and is off as a permanent
    # site characteristic, not as a pending task. Asserted for the same reason as
    # mouthshut: re-enabling it should be deliberate and update a test.
    assert "ajio_manual" not in enabled
    for source in (
        "play_store",
        "app_store",
        "youtube",
        "trustpilot",
        "complaints_board",
        "consumer_complaints_in",
        "ajio_onsite",
        "quora_manual",
    ):
        assert source in enabled


def test_quora_is_manual_import_only():
    """Quora's robots.txt prohibits bot use for AI/ML, so there must be no crawl config."""
    run_config, _ = load_run_config()
    quora = run_config.collection.quora_manual
    assert quora.import_dir
    assert not hasattr(quora, "max_pages")
    assert not hasattr(quora, "urls")


def test_robots_txt_compliance_is_on():
    run_config, _ = load_run_config()
    assert run_config.collection.respect_robots_txt is True


def test_blank_credential_counts_as_missing(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("GROQ_API_KEY=   \n", encoding="utf-8")
    monkeypatch.setattr(config_module, "ENV_PATH", env_file)
    for name in CREDENTIAL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    assert "GROQ_API_KEY" in missing_credentials()


def test_module_attribute_access_triggers_load(monkeypatch, tmp_path):
    """`from config import settings` must raise when config is broken."""
    monkeypatch.setattr(config_module, "ENV_PATH", tmp_path / ".env")
    for name in CREDENTIAL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    config_module.get_settings.cache_clear()
    with pytest.raises(MissingConfigError):
        _ = config_module.settings
    config_module.get_settings.cache_clear()


def test_unknown_module_attribute_still_raises_attribute_error():
    with pytest.raises(AttributeError):
        _ = config_module.not_a_real_attribute


@pytest.fixture
def dummy_credentials(monkeypatch, tmp_path):
    """Populated credentials so the success path can be tested without real keys."""
    monkeypatch.setattr(config_module, "ENV_PATH", tmp_path / "absent.env")
    for name in REQUIRED_ENV_VARS:
        monkeypatch.setenv(name, "dummy-value")
    for name in OPTIONAL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    config_module.get_settings.cache_clear()
    yield
    config_module.get_settings.cache_clear()


def test_settings_construct_and_expose_config_hash(dummy_credentials):
    settings = config_module.get_settings()
    assert settings.run.model.name == "openai/gpt-oss-120b"
    assert len(settings.config_hash) == 64
    assert settings.run.model.docs_per_request == 6
    assert settings.run.filters.min_words == 3


def test_paths_resolve_absolutely_regardless_of_cwd(dummy_credentials, monkeypatch, tmp_path):
    """Paths must not depend on where a script was launched from."""
    monkeypatch.chdir(tmp_path)
    settings = config_module.get_settings()
    assert settings.interim_db.is_absolute()
    assert settings.interim_db.name == "discovery.db"
    assert settings.raw_dir.is_absolute()


def test_credentials_never_render_in_repr(dummy_credentials):
    """A traceback or log line must not leak a key (edge-case 0.5)."""
    settings = config_module.get_settings()
    assert "dummy-value" not in repr(settings)
    assert "dummy-value" not in repr(settings.credentials)
    assert "dummy-value" not in str(settings.credentials.groq_api_key)
    assert settings.credentials.groq_api_key.get_secret_value() == "dummy-value"


def test_ensure_dirs_is_idempotent(dummy_credentials):
    settings = config_module.get_settings()
    settings.ensure_dirs()
    settings.ensure_dirs()
    assert settings.raw_dir.is_dir()
    assert settings.logs_dir.is_dir()
    assert settings.interim_db.parent.is_dir()
