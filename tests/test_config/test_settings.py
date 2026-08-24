"""Tests for the configuration loader."""

from pathlib import Path

import pytest

from config.settings import (
    DEFAULT_CONFIG_PATH,
    PROJECT_ROOT,
    AppConfig,
    ConfigError,
    EnvSettings,
    anchor_path,
    get_settings,
    load_config,
    reload_settings,
)


class TestLoadConfig:
    def test_parses_the_committed_config_yaml(self):
        config = load_config()

        assert config.llm.provider_mode == "mixed"
        assert config.chunking.target_chunk_size_tokens == 400
        assert config.chunking.chunk_overlap_tokens == 40
        assert config.retrieval.top_k_vector == 8
        assert config.retrieval.relationship_candidate_count == 10
        assert config.embedding.model == "sentence-transformers/all-MiniLM-L6-v2"

    def test_parses_nested_filter_rules(self):
        config = load_config()

        assert config.filters.browser_history.min_visit_count == 2
        assert "facebook.com" in config.filters.browser_history.domain_blocklist
        assert "CATEGORY_PROMOTIONS" in config.filters.gmail.excluded_labels
        assert config.filters.min_content_length == 20

    def test_missing_file_raises_config_error(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="not found"):
            load_config(tmp_path / "nope.yaml")

    def test_malformed_yaml_raises_config_error(self, tmp_path: Path):
        bad = tmp_path / "config.yaml"
        bad.write_text("llm: [unclosed", encoding="utf-8")

        with pytest.raises(ConfigError, match="Could not parse"):
            load_config(bad)

    def test_non_mapping_yaml_raises_config_error(self, tmp_path: Path):
        bad = tmp_path / "config.yaml"
        bad.write_text("- just\n- a list\n", encoding="utf-8")

        with pytest.raises(ConfigError, match="mapping"):
            load_config(bad)

    def test_empty_file_falls_back_to_defaults(self, tmp_path: Path):
        empty = tmp_path / "config.yaml"
        empty.write_text("", encoding="utf-8")

        assert load_config(empty) == AppConfig()

    def test_partial_file_keeps_defaults_for_omitted_sections(self, tmp_path: Path):
        partial = tmp_path / "config.yaml"
        partial.write_text("llm:\n  provider_mode: fully_local\n", encoding="utf-8")

        config = load_config(partial)

        assert config.llm.provider_mode == "fully_local"
        assert config.llm.local_model == "llama3:8b"
        assert config.retrieval.top_k_vector == 8

    def test_invalid_provider_mode_is_rejected(self, tmp_path: Path):
        bad = tmp_path / "config.yaml"
        bad.write_text("llm:\n  provider_mode: sometimes\n", encoding="utf-8")

        with pytest.raises(ValueError):
            load_config(bad)

    def test_committed_config_path_exists(self):
        assert DEFAULT_CONFIG_PATH.exists()


class TestEnvSettings:
    def test_watch_dirs_splits_on_commas_and_strips_whitespace(self):
        env = EnvSettings(local_files_watch_dirs="C:/notes, C:/papers ,C:/code")

        assert env.watch_dirs == [
            Path("C:/notes"),
            Path("C:/papers"),
            Path("C:/code"),
        ]

    def test_watch_dirs_is_empty_when_unset(self):
        assert EnvSettings(local_files_watch_dirs="").watch_dirs == []

    def test_watch_dirs_ignores_empty_segments(self):
        env = EnvSettings(local_files_watch_dirs="C:/notes,,  ,")

        assert env.watch_dirs == [Path("C:/notes")]

    def test_reads_values_from_the_process_environment(self, monkeypatch):
        monkeypatch.setenv("NEO4J_URI", "bolt://example:9999")
        monkeypatch.setenv("FASTAPI_PORT", "9000")

        env = EnvSettings()

        assert env.neo4j_uri == "bolt://example:9999"
        assert env.fastapi_port == 9000

    def test_environment_lookup_is_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("log_level", "DEBUG")

        assert EnvSettings().log_level == "DEBUG"

    def test_unknown_variables_are_ignored(self, monkeypatch):
        monkeypatch.setenv("SOMETHING_UNRELATED", "value")

        EnvSettings()  # must not raise


class TestBlankValuesAreTreatedAsUnset:
    """A freshly copied .env.example leaves every variable blank."""

    def test_blank_optional_secret_stays_none(self, monkeypatch):
        monkeypatch.setenv("NOTION_API_KEY", "")

        assert EnvSettings().notion_api_key is None

    def test_blank_optional_path_stays_none_rather_than_dot(self, monkeypatch):
        monkeypatch.setenv("GMAIL_CREDENTIALS_PATH", "")

        assert EnvSettings().gmail_credentials_path is None

    def test_whitespace_only_value_stays_unset(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "   ")

        assert EnvSettings().github_token is None

    def test_blank_value_falls_back_to_documented_default(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_HOST", "")
        monkeypatch.setenv("LOG_LEVEL", "")

        env = EnvSettings()

        assert env.ollama_host == "http://localhost:11434"
        assert env.log_level == "INFO"

    def test_a_real_value_is_still_read(self, monkeypatch):
        monkeypatch.setenv("NOTION_API_KEY", "secret_abc123")

        assert EnvSettings().notion_api_key == "secret_abc123"


class TestPathsAreAnchoredToProjectRoot:
    """Scheduled runs have a different CWD than manual ones."""

    def test_relative_path_resolves_against_the_repository_root(self, monkeypatch):
        monkeypatch.setenv("SQLITE_DB_PATH", "./data/pkg_agent.db")

        db_path = EnvSettings().sqlite_db_path

        assert db_path.is_absolute()
        assert db_path == (PROJECT_ROOT / "data" / "pkg_agent.db").resolve()

    def test_absolute_path_is_left_alone(self, monkeypatch):
        absolute = Path("C:/elsewhere/pkg.db").resolve()
        monkeypatch.setenv("SQLITE_DB_PATH", str(absolute))

        assert EnvSettings().sqlite_db_path == absolute

    def test_defaults_are_already_absolute(self):
        env = EnvSettings()

        assert env.sqlite_db_path.is_absolute()
        assert env.chroma_persist_dir.is_absolute()

    def test_optional_paths_are_anchored_too(self, monkeypatch):
        monkeypatch.setenv("BROWSER_HISTORY_PATH", "data/History")

        assert (
            EnvSettings().browser_history_path
            == (PROJECT_ROOT / "data" / "History").resolve()
        )

    def test_watch_dirs_are_anchored(self):
        env = EnvSettings(local_files_watch_dirs="notes, C:/papers")

        assert env.watch_dirs[0] == (PROJECT_ROOT / "notes").resolve()
        assert env.watch_dirs[1].is_absolute()

    def test_anchor_path_helper_is_idempotent(self):
        once = anchor_path(Path("data/pkg.db"))

        assert anchor_path(once) == once


class TestGetSettings:
    def test_returns_both_halves(self):
        settings = get_settings()

        assert settings.config.llm.provider_mode == "mixed"
        assert settings.env.fastapi_port > 0

    def test_result_is_cached(self):
        assert get_settings() is get_settings()

    def test_reload_clears_the_cache(self):
        first = get_settings()
        second = reload_settings()

        assert first is not second
        assert second is get_settings()
