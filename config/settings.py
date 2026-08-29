"""Typed access to ``config/config.yaml`` and the ``config/.env`` file.

This module is the single place the rest of the system reads configuration from.
It belongs to the configuration layer and imports nothing from any other layer,
so every layer may depend on it without violating the one-way dependency rule
defined in ``docs/Component_Map.docx``.

Typical use::

    from config.settings import get_settings

    settings = get_settings()
    db_path = settings.env.sqlite_db_path
    top_k = settings.config.retrieval.top_k_vector
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from ruamel.yaml import YAML

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
DEFAULT_CONFIG_PATH = CONFIG_DIR / "config.yaml"
DEFAULT_ENV_PATH = CONFIG_DIR / ".env"

ProviderMode = Literal["fully_local", "fully_cloud", "mixed"]


class ConfigError(Exception):
    """Raised when configuration is missing, unreadable, or invalid."""


def anchor_path(value: Path) -> Path:
    """Resolve a configured path against the repository root.

    Relative paths in ``.env`` are anchored to ``PROJECT_ROOT`` rather than the
    process working directory, so the daily batch resolves the same paths when
    launched by cron or Task Scheduler as it does when run by hand from the
    repository root. Absolute paths are returned unchanged apart from
    normalization.

    Args:
        value: A path from configuration, absolute or relative.

    Returns:
        An absolute path.
    """
    return value.resolve() if value.is_absolute() else (PROJECT_ROOT / value).resolve()


# ---------------------------------------------------------------------------
# config.yaml — non-secret settings
# ---------------------------------------------------------------------------


class LLMConfig(BaseModel):
    """LLM provider selection and the models used on each side."""

    provider_mode: ProviderMode = "mixed"
    local_model: str = "llama3:8b"
    cloud_model: str = "anthropic/claude-sonnet-4"
    cloud_max_tokens: int = 4096


class IngestionConfig(BaseModel):
    """Daily batch scheduling and batching behaviour."""

    schedule: str = "0 23 * * *"
    batch_metadata_group_size: int = 10


class BrowserHistoryFilters(BaseModel):
    """Noise filtering rules specific to the browser history source."""

    min_visit_count: int = 2
    domain_blocklist: list[str] = Field(default_factory=list)


class GmailFilters(BaseModel):
    """Noise filtering rules specific to the Gmail source."""

    excluded_labels: list[str] = Field(default_factory=list)


class FiltersConfig(BaseModel):
    """Per-source noise filtering rules, plus the cross-source content filter."""

    browser_history: BrowserHistoryFilters = Field(
        default_factory=BrowserHistoryFilters
    )
    gmail: GmailFilters = Field(default_factory=GmailFilters)
    min_content_length: int = 20


class ChunkingConfig(BaseModel):
    """Chunk sizing used when splitting item text."""

    target_chunk_size_tokens: int = 400
    chunk_overlap_tokens: int = 40


class RetrievalConfig(BaseModel):
    """Result counts used by the retrieval nodes and relationship detection."""

    top_k_vector: int = 8
    top_k_keyword: int = 8
    relationship_candidate_count: int = 10
    relationship_confidence_threshold: float = 0.6
    relationship_candidate_max_distance: float | None = 0.6


class EmbeddingConfig(BaseModel):
    """Local embedding model selection."""

    model: str = "sentence-transformers/all-MiniLM-L6-v2"


class AppConfig(BaseModel):
    """Full contents of ``config/config.yaml``."""

    llm: LLMConfig = Field(default_factory=LLMConfig)
    ingestion: IngestionConfig = Field(default_factory=IngestionConfig)
    filters: FiltersConfig = Field(default_factory=FiltersConfig)
    chunking: ChunkingConfig = Field(default_factory=ChunkingConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)


# ---------------------------------------------------------------------------
# .env — secrets and machine-specific paths
# ---------------------------------------------------------------------------


class EnvSettings(BaseSettings):
    """Environment variables read from ``config/.env`` and the process environment.

    Every field is optional so that partially configured machines (for example,
    one running in ``fully_local`` provider mode with no OpenRouter key) still
    load. Components validate the specific variables they need at point of use.
    """

    model_config = SettingsConfigDict(
        env_file=DEFAULT_ENV_PATH,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Source API credentials
    notion_api_key: str | None = None
    gmail_credentials_path: Path | None = None
    github_token: str | None = None
    google_calendar_credentials_path: Path | None = None
    browser_history_path: Path | None = None
    local_files_watch_dirs: str = ""

    # LLM provider credentials
    openrouter_api_key: str | None = None
    ollama_host: str = "http://localhost:11434"

    # Storage connection settings
    sqlite_db_path: Path = PROJECT_ROOT / "data" / "pkg_agent.db"
    chroma_persist_dir: Path = PROJECT_ROOT / "data" / "chroma"
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str | None = None

    # Application settings
    log_level: str = "INFO"
    fastapi_port: int = 8080
    langsmith_api_key: str | None = None
    langsmith_project: str = "personal-knowledge-graph-agent"

    @model_validator(mode="before")
    @classmethod
    def _blank_is_unset(cls, data: Any) -> Any:
        """Drop blank values so field defaults apply instead of empty strings.

        ``config/.env.example`` ships every variable with an empty value, so a
        freshly copied ``.env`` would otherwise yield ``""`` for unset secrets
        and ``Path(".")`` for unset paths — both of which read as configured to
        any caller checking for ``None``.
        """
        if isinstance(data, dict):
            return {
                key: value
                for key, value in data.items()
                if not (isinstance(value, str) and not value.strip())
            }
        return data

    @field_validator(
        "gmail_credentials_path",
        "google_calendar_credentials_path",
        "browser_history_path",
        "sqlite_db_path",
        "chroma_persist_dir",
    )
    @classmethod
    def _anchor_to_project_root(cls, value: Path | None) -> Path | None:
        """Make every configured path absolute and independent of the CWD."""
        return None if value is None else anchor_path(value)

    @property
    def watch_dirs(self) -> list[Path]:
        """Parse ``LOCAL_FILES_WATCH_DIRS`` into a list of absolute paths.

        Returns:
            The configured watch directories, empty if the variable is unset.
        """
        return [
            anchor_path(Path(part.strip()))
            for part in self.local_files_watch_dirs.split(",")
            if part.strip()
        ]


class Settings(BaseModel):
    """Both configuration sources, resolved together."""

    env: EnvSettings
    config: AppConfig


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> AppConfig:
    """Load and validate ``config.yaml``.

    Args:
        path: Path to the YAML configuration file.

    Returns:
        The parsed configuration.

    Raises:
        ConfigError: If the file is missing or is not valid YAML mapping.
    """
    if not path.exists():
        raise ConfigError(f"Configuration file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Could not parse {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"Expected a mapping at the top level of {path}")
    return AppConfig.model_validate(raw)


def update_llm_config(
    *,
    provider_mode: ProviderMode | None = None,
    local_model: str | None = None,
    cloud_model: str | None = None,
    path: Path = DEFAULT_CONFIG_PATH,
) -> AppConfig:
    """Update ``config.yaml``'s ``llm`` section on disk, in place.

    Used by ``PUT /api/settings``. Only the given fields are changed; the
    rest of the file — including every comment and the ``llm`` block's own
    unset fields — is left untouched, via ``ruamel.yaml``'s round-trip
    mode rather than a plain re-serialize (which would silently strip
    every comment in the file, including the ones documenting each
    setting's valid values — see ``DECISIONS.md``).

    Args:
        provider_mode: New provider mode, or ``None`` to leave unchanged.
        local_model: New local model tag, or ``None`` to leave unchanged.
        cloud_model: New cloud model id, or ``None`` to leave unchanged.
        path: Path to the YAML configuration file. Defaults to the real
            configuration file; passing a different path (tests) writes
            there instead and leaves the process-wide ``get_settings()``
            cache untouched.

    Returns:
        The freshly reloaded configuration, reflecting the write. Read
        back from ``path`` itself, not assumed from the in-memory update,
        so this also confirms the write actually parses correctly.

    Raises:
        ConfigError: If the file can't be read/parsed, or the resulting
            ``llm`` section fails validation — checked *before* anything is
            written to disk, so a bad update never corrupts the file.
    """
    yaml_rt = YAML()
    yaml_rt.preserve_quotes = True
    yaml_rt.indent(mapping=2, sequence=4, offset=2)
    try:
        with path.open(encoding="utf-8") as f:
            data = yaml_rt.load(f)
    except OSError as exc:
        raise ConfigError(f"Could not read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Expected a mapping at the top level of {path}")

    llm_section = data.get("llm") or {}
    updated = dict(llm_section)
    if provider_mode is not None:
        updated["provider_mode"] = provider_mode
    if local_model is not None:
        updated["local_model"] = local_model
    if cloud_model is not None:
        updated["cloud_model"] = cloud_model

    try:
        LLMConfig.model_validate(updated)
    except ValidationError as exc:
        raise ConfigError(f"Invalid LLM configuration: {exc}") from exc

    for key, value in updated.items():
        llm_section[key] = value
    data["llm"] = llm_section

    try:
        with path.open("w", encoding="utf-8") as f:
            yaml_rt.dump(data, f)
    except OSError as exc:
        raise ConfigError(f"Could not write {path}: {exc}") from exc

    # reload_settings()/get_settings() are cached against DEFAULT_CONFIG_PATH
    # specifically. Only invalidate that global cache when this write
    # actually targeted it — a caller using a non-default `path` (tests)
    # gets that file's own freshly parsed config back, without silently
    # refreshing the process-wide cache from an unrelated file.
    if path == DEFAULT_CONFIG_PATH:
        return reload_settings().config
    return load_config(path)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, loading them on first call.

    The result is cached; call :func:`reload_settings` after changing
    configuration at runtime (for example via ``PUT /api/settings``).

    Returns:
        The resolved environment and file configuration.
    """
    return Settings(env=EnvSettings(), config=load_config())


def reload_settings() -> Settings:
    """Clear the settings cache and reload from disk.

    Returns:
        The freshly loaded settings.
    """
    get_settings.cache_clear()
    return get_settings()
