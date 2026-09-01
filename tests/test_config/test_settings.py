"""Tests for the configuration loader."""

from datetime import date, timedelta
from pathlib import Path

import pytest

from config.settings import (
    DEFAULT_CONFIG_PATH,
    PROJECT_ROOT,
    AppConfig,
    ConfigError,
    EnvSettings,
    _retry_on_transient_permission_error,
    anchor_path,
    get_settings,
    load_config,
    reload_settings,
    update_llm_config,
    update_source_config,
)


class TestLoadConfig:
    def test_parses_the_committed_config_yaml(self):
        config = load_config()

        assert config.llm.provider_mode == "fully_cloud"
        assert config.chunking.target_chunk_size_tokens == 400
        assert config.chunking.chunk_overlap_tokens == 40
        assert config.retrieval.top_k_vector == 8
        assert config.retrieval.relationship_candidate_count == 10
        assert config.llm.cloud_embedding_model == "openai/text-embedding-3-small"

    def test_llm_config_has_no_local_embedding_model_field(self):
        """There is no local embedding path — see LLMConfig's docstring."""
        config = load_config()

        assert not hasattr(config.llm, "local_embedding_model")

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
        assert config.llm.local_generation_model == "llama3:8b"
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


class TestEffectiveDateRanges:
    """Gmail/Calendar default to a rolling window when unset.

    Unlike watch_dirs/notion_page_ids_list/github_repos_list, "unset" here
    never means "unbounded." GitHub's own date range keeps the old
    unset-means-unbounded behavior; only Gmail and Calendar were asked to
    default. See DECISIONS.md.
    """

    def test_gmail_defaults_to_the_last_15_days_when_unset(self, monkeypatch):
        # Explicitly blanked rather than relying on the real config/.env
        # happening to leave these unset — see
        # test_github_still_has_no_default_when_unset's docstring.
        monkeypatch.setenv("GMAIL_DATE_RANGE_START", "")
        monkeypatch.setenv("GMAIL_DATE_RANGE_END", "")
        env = EnvSettings()

        assert env.effective_gmail_date_range_start == date.today() - timedelta(days=15)
        assert env.effective_gmail_date_range_end == date.today()

    def test_gmail_uses_the_configured_range_when_set(self):
        env = EnvSettings(
            gmail_date_range_start="2026-01-01", gmail_date_range_end="2026-01-31"
        )

        assert env.effective_gmail_date_range_start == date(2026, 1, 1)
        assert env.effective_gmail_date_range_end == date(2026, 1, 31)

    def test_calendar_defaults_to_today_through_30_days_out_when_unset(
        self, monkeypatch
    ):
        monkeypatch.setenv("CALENDAR_DATE_RANGE_START", "")
        monkeypatch.setenv("CALENDAR_DATE_RANGE_END", "")
        env = EnvSettings()

        assert env.effective_calendar_date_range_start == date.today()
        assert env.effective_calendar_date_range_end == date.today() + timedelta(
            days=30
        )

    def test_calendar_uses_the_configured_range_when_set(self):
        env = EnvSettings(
            calendar_date_range_start="2026-03-01",
            calendar_date_range_end="2026-03-15",
        )

        assert env.effective_calendar_date_range_start == date(2026, 3, 1)
        assert env.effective_calendar_date_range_end == date(2026, 3, 15)

    def test_github_still_has_no_default_when_unset(self, monkeypatch):
        """Confirm this change is scoped to Gmail/Calendar only.

        GitHub's own date range was never asked to default. Explicitly
        blanks the two env vars rather than relying on ``EnvSettings()``'s
        bare default — the real ``config/.env`` may genuinely have a
        GitHub range configured (e.g. from manual testing), which would
        otherwise make this test's result depend on local machine state.
        """
        monkeypatch.setenv("GITHUB_DATE_RANGE_START", "")
        monkeypatch.setenv("GITHUB_DATE_RANGE_END", "")
        env = EnvSettings()

        assert env.github_date_range_start is None
        assert env.github_date_range_end is None


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


_SAMPLE_CONFIG = """\
# Non-secret configuration for the Personal Knowledge Graph Agent.
# Reference: docs/Environment_Config_Reference.docx section 4.

llm:
  provider_mode: fully_local # fully_local | fully_cloud
  local_generation_model: llama3:8b # used when fully_local
  cloud_generation_model: anthropic/claude-sonnet-4 # used when fully_cloud

ingestion:
  schedule: "0 23 * * *" # cron expression; default 11 PM daily
  batch_metadata_group_size: 10

filters:
  min_content_length: 20
  browser_history:
    min_visit_count: 2
    domain_blocklist:
      - google.com/search
      - facebook.com

chunking:
  target_chunk_size_tokens: 400
  chunk_overlap_tokens: 40

retrieval:
  top_k_vector: 8
  top_k_keyword: 8
  relationship_candidate_count: 10
  relationship_confidence_threshold: 0.6
"""


class TestUpdateLlmConfig:
    def test_updates_only_the_given_fields(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(_SAMPLE_CONFIG, encoding="utf-8")

        result = update_llm_config(provider_mode="fully_cloud", path=config_path)

        assert result.llm.provider_mode == "fully_cloud"
        assert result.llm.cloud_generation_model == "anthropic/claude-sonnet-4"
        assert result.llm.local_generation_model == "llama3:8b"

    def test_updates_multiple_fields_at_once(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(_SAMPLE_CONFIG, encoding="utf-8")

        result = update_llm_config(
            provider_mode="fully_cloud",
            cloud_generation_model="openai/gpt-4o",
            path=config_path,
        )

        assert result.llm.provider_mode == "fully_cloud"
        assert result.llm.cloud_generation_model == "openai/gpt-4o"

    def test_the_written_file_is_actually_updated(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(_SAMPLE_CONFIG, encoding="utf-8")

        update_llm_config(cloud_generation_model="openai/gpt-4o", path=config_path)

        assert "openai/gpt-4o" in config_path.read_text(encoding="utf-8")

    def test_comments_and_other_sections_are_preserved(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(_SAMPLE_CONFIG, encoding="utf-8")

        update_llm_config(provider_mode="fully_cloud", path=config_path)

        written = config_path.read_text(encoding="utf-8")
        assert "# fully_local | fully_cloud" in written
        assert '"0 23 * * *"' in written
        assert "domain_blocklist:" in written
        assert "google.com/search" in written

    def test_local_embedding_model_is_not_an_accepted_parameter(self, tmp_path):
        """There is no local embedding path.

        The function signature has no parameter for one, unlike
        cloud_embedding_model.
        """
        config_path = tmp_path / "config.yaml"
        config_path.write_text(_SAMPLE_CONFIG, encoding="utf-8")

        with pytest.raises(TypeError):
            update_llm_config(local_embedding_model="x", path=config_path)

    def test_updates_the_cloud_embedding_model(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(_SAMPLE_CONFIG, encoding="utf-8")

        result = update_llm_config(
            cloud_embedding_model="openai/text-embedding-3-large", path=config_path
        )

        assert result.llm.cloud_embedding_model == "openai/text-embedding-3-large"

    def test_invalid_provider_mode_raises_and_does_not_write(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(_SAMPLE_CONFIG, encoding="utf-8")
        original = config_path.read_text(encoding="utf-8")

        with pytest.raises(ConfigError):
            update_llm_config(provider_mode="not_a_real_mode", path=config_path)

        assert config_path.read_text(encoding="utf-8") == original

    def test_a_non_default_path_does_not_touch_the_global_settings_cache(
        self, tmp_path
    ):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(_SAMPLE_CONFIG, encoding="utf-8")
        before = get_settings()

        update_llm_config(provider_mode="fully_cloud", path=config_path)

        assert get_settings() is before


class TestRetryOnTransientPermissionError:
    """Tests for the transient-permission-error retry helper.

    Windows can transiently deny a file rename/replace if another process
    (an antivirus scanner, the search indexer, an editor) has briefly
    opened the target — see DECISIONS.md for how this was root-caused
    against a real WinError 5 report.
    """

    def test_returns_the_result_on_first_success(self):
        assert _retry_on_transient_permission_error(lambda: "ok") == "ok"

    def test_retries_and_succeeds_after_transient_failures(self, monkeypatch):
        monkeypatch.setattr("config.settings.time.sleep", lambda _seconds: None)
        calls = {"count": 0}

        def flaky():
            calls["count"] += 1
            if calls["count"] < 3:
                raise PermissionError("WinError 5: Access is denied")
            return "eventually ok"

        assert _retry_on_transient_permission_error(flaky) == "eventually ok"
        assert calls["count"] == 3

    def test_raises_after_exhausting_every_attempt(self, monkeypatch):
        monkeypatch.setattr("config.settings.time.sleep", lambda _seconds: None)

        def always_fails():
            raise PermissionError("WinError 5: Access is denied")

        with pytest.raises(PermissionError):
            _retry_on_transient_permission_error(always_fails)

    def test_a_non_permission_error_is_not_retried(self, monkeypatch):
        monkeypatch.setattr("config.settings.time.sleep", lambda _seconds: None)
        calls = {"count": 0}

        def raises_something_else():
            calls["count"] += 1
            raise ValueError("not a permission error")

        with pytest.raises(ValueError):
            _retry_on_transient_permission_error(raises_something_else)
        assert calls["count"] == 1


class TestUpdateSourceConfig:
    def test_updates_watch_dirs(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("SOME_OTHER_KEY=unchanged\n", encoding="utf-8")

        result = update_source_config(
            local_files_watch_dirs=["/a/b", "/c/d"], path=env_path
        )

        assert [str(p) for p in result.watch_dirs] == [
            str(anchor_path(Path("/a/b"))),
            str(anchor_path(Path("/c/d"))),
        ]

    def test_updates_notion_page_ids(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("SOME_OTHER_KEY=unchanged\n", encoding="utf-8")

        result = update_source_config(
            notion_page_ids=["page-1", "page-2"], path=env_path
        )

        assert result.notion_page_ids_list == ["page-1", "page-2"]

    def test_updates_github_repos(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("SOME_OTHER_KEY=unchanged\n", encoding="utf-8")

        result = update_source_config(
            github_repos=["me/repo-a", "me/repo-b"], path=env_path
        )

        assert result.github_repos_list == ["me/repo-a", "me/repo-b"]

    def test_an_empty_github_repos_list_clears_the_setting(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("SOME_OTHER_KEY=unchanged\n", encoding="utf-8")
        update_source_config(github_repos=["me/repo-a"], path=env_path)

        result = update_source_config(github_repos=[], path=env_path)

        assert result.github_repos_list == []

    def test_updates_date_range_fields(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("SOME_OTHER_KEY=unchanged\n", encoding="utf-8")

        result = update_source_config(
            gmail_date_range_start="2026-01-01",
            gmail_date_range_end="2026-06-30",
            github_date_range_start="2025-01-01",
            github_date_range_end="2025-12-31",
            calendar_date_range_start="2026-03-01",
            calendar_date_range_end="2026-03-31",
            path=env_path,
        )

        assert result.gmail_date_range_start == date(2026, 1, 1)
        assert result.gmail_date_range_end == date(2026, 6, 30)
        assert result.github_date_range_start == date(2025, 1, 1)
        assert result.github_date_range_end == date(2025, 12, 31)
        assert result.calendar_date_range_start == date(2026, 3, 1)
        assert result.calendar_date_range_end == date(2026, 3, 31)

    def test_an_empty_string_clears_a_date_range_field(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("SOME_OTHER_KEY=unchanged\n", encoding="utf-8")
        update_source_config(gmail_date_range_start="2026-01-01", path=env_path)

        result = update_source_config(gmail_date_range_start="", path=env_path)

        assert result.gmail_date_range_start is None

    def test_a_none_date_range_field_is_left_unchanged(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("SOME_OTHER_KEY=unchanged\n", encoding="utf-8")
        update_source_config(gmail_date_range_start="2026-01-01", path=env_path)

        result = update_source_config(notion_page_ids=["page-1"], path=env_path)

        assert result.gmail_date_range_start == date(2026, 1, 1)

    def test_omitted_fields_are_left_unchanged(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("SOME_OTHER_KEY=unchanged\n", encoding="utf-8")
        update_source_config(local_files_watch_dirs=["/a"], path=env_path)

        result = update_source_config(notion_page_ids=["page-1"], path=env_path)

        assert [str(p) for p in result.watch_dirs] == [str(anchor_path(Path("/a")))]
        assert result.notion_page_ids_list == ["page-1"]

    def test_an_empty_list_clears_the_setting(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("SOME_OTHER_KEY=unchanged\n", encoding="utf-8")
        update_source_config(notion_page_ids=["page-1"], path=env_path)

        result = update_source_config(notion_page_ids=[], path=env_path)

        assert result.notion_page_ids_list == []

    def test_other_lines_in_the_file_are_preserved(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text(
            "NOTION_API_KEY=secret-123\n# a comment\n", encoding="utf-8"
        )

        update_source_config(local_files_watch_dirs=["/a"], path=env_path)

        written = env_path.read_text(encoding="utf-8")
        assert "NOTION_API_KEY=secret-123" in written
        assert "# a comment" in written

    def test_paths_with_backslashes_and_commas_round_trip_correctly(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("SOME_OTHER_KEY=unchanged\n", encoding="utf-8")
        windows_path = r"C:\Users\Test\Documents\Watched"

        result = update_source_config(
            local_files_watch_dirs=[windows_path], path=env_path
        )

        assert str(result.watch_dirs[0]) == str(anchor_path(Path(windows_path)))

    def test_a_trailing_backslash_does_not_corrupt_the_rest_of_the_file(self, tmp_path):
        r"""Real-world regression: a path ending in a backslash.

        (e.g. from a folder-picker returning a drive/project root) breaks
        python-dotenv's own write-then-read round trip — it single-quotes
        the value but only escapes literal quote characters, not
        backslashes, so the trailing "\" lands right before the closing
        quote and its parser reads that as an escaped quote instead of the
        string ending, corrupting every line after it too. Verified
        directly against a real .env that silently lost 12 keys, including
        OPENROUTER_API_KEY, this way.
        """
        env_path = tmp_path / ".env"
        env_path.write_text(
            "OPENROUTER_API_KEY=sk-test-should-survive\n", encoding="utf-8"
        )
        trailing_backslash_path = "C:\\Users\\Test\\Documents\\Watched" + "\\"

        update_source_config(
            local_files_watch_dirs=[trailing_backslash_path], path=env_path
        )

        from dotenv import dotenv_values

        values = dotenv_values(env_path)
        assert values.get("OPENROUTER_API_KEY") == "sk-test-should-survive"
        assert (
            values.get("LOCAL_FILES_WATCH_DIRS")
            == "C:\\Users\\Test\\Documents\\Watched"
        )

    def test_a_non_default_path_does_not_touch_the_global_settings_cache(
        self, tmp_path
    ):
        env_path = tmp_path / ".env"
        env_path.write_text("SOME_OTHER_KEY=unchanged\n", encoding="utf-8")
        before = get_settings()

        update_source_config(local_files_watch_dirs=["/a"], path=env_path)

        assert get_settings() is before

    def test_a_transient_permission_error_is_retried_and_recovers(
        self, tmp_path, monkeypatch
    ):
        """Real-world regression: a user hit exactly this.

        WinError 5 on the rename set_key() does internally, while saving a
        changed watch folder. Verified directly that a retry resolves it;
        this confirms update_source_config() actually retries rather than
        failing on the first transient denial.
        """
        monkeypatch.setattr("config.settings.time.sleep", lambda _seconds: None)
        env_path = tmp_path / ".env"
        env_path.write_text("SOME_OTHER_KEY=unchanged\n", encoding="utf-8")

        import dotenv

        real_set_key = dotenv.set_key
        calls = {"count": 0}

        def flaky_set_key(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise PermissionError(
                    "[WinError 5] Access is denied: '.tmp_xyz' -> '.env'"
                )
            return real_set_key(*args, **kwargs)

        monkeypatch.setattr(dotenv, "set_key", flaky_set_key)

        result = update_source_config(local_files_watch_dirs=["/a/b"], path=env_path)

        assert calls["count"] == 2
        assert [str(p) for p in result.watch_dirs] == [str(anchor_path(Path("/a/b")))]


class TestGetSettings:
    def test_returns_both_halves(self):
        settings = get_settings()

        assert settings.config.llm.provider_mode == "fully_cloud"
        assert settings.env.fastapi_port > 0

    def test_result_is_cached(self):
        assert get_settings() is get_settings()

    def test_reload_clears_the_cache(self):
        first = get_settings()
        second = reload_settings()

        assert first is not second
        assert second is get_settings()
