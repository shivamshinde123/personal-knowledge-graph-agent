"""``GET``/``PUT /api/settings``: the current LLM provider configuration.

Per ``docs/API_Specification.docx`` sections 3.6/3.7. Reads/writes
``config/settings.py`` directly rather than via an ``agent/`` module —
``api/__init__.py``'s "never reaching into storage or providers directly"
rule names those two layers specifically; ``config`` is the shared
configuration layer every other layer (including ``agent/``) already
depends on directly, not a layer this rule is about. See ``DECISIONS.md``.
"""

from __future__ import annotations

from fastapi import APIRouter

from agent.browse import browse_folder
from api.schemas import (
    BrowseFolderResponse,
    SettingsResponse,
    SettingsUpdateRequest,
    SettingsUpdateResponse,
    SourceConfigResponse,
    SourceConfigUpdateRequest,
)
from config.settings import get_settings, update_llm_config, update_source_config

router = APIRouter()


@router.get("/settings", response_model=SettingsResponse)
def get_settings_route() -> SettingsResponse:
    """Return the current LLM provider configuration."""
    llm = get_settings().config.llm
    return SettingsResponse(
        provider_mode=llm.provider_mode,
        local_generation_model=llm.local_generation_model,
        cloud_generation_model=llm.cloud_generation_model,
        cloud_embedding_model=llm.cloud_embedding_model,
    )


@router.put("/settings", response_model=SettingsUpdateResponse)
def put_settings_route(payload: SettingsUpdateRequest) -> SettingsUpdateResponse:
    """Update the LLM provider configuration; a ``ConfigError`` maps to 500.

    A ``cloud_embedding_model`` change isn't specially handled here — the
    frontend is responsible for confirming with the user and triggering a
    reset + re-ingest afterward (see ``SettingsResponse``'s docstring).
    """
    config = update_llm_config(
        provider_mode=payload.provider_mode,
        local_generation_model=payload.local_generation_model,
        cloud_generation_model=payload.cloud_generation_model,
        cloud_embedding_model=payload.cloud_embedding_model,
    )
    return SettingsUpdateResponse(
        status="updated",
        provider_mode=config.llm.provider_mode,
        local_generation_model=config.llm.local_generation_model,
        cloud_generation_model=config.llm.cloud_generation_model,
        cloud_embedding_model=config.llm.cloud_embedding_model,
    )


@router.get("/settings/sources", response_model=SourceConfigResponse)
def get_source_config_route() -> SourceConfigResponse:
    """Return the current source-scope configuration (watch folders, Notion scope)."""
    env = get_settings().env
    return SourceConfigResponse(
        local_files_watch_dirs=[str(d) for d in env.watch_dirs],
        notion_page_ids=env.notion_page_ids_list,
    )


@router.post("/settings/browse-folder", response_model=BrowseFolderResponse)
def browse_folder_route() -> BrowseFolderResponse:
    """Open a native folder-picker dialog on the server machine.

    Only meaningful because this backend runs on the same local machine as
    the user — see ``agent/browse.py``. Blocks until the dialog is closed;
    ``path`` is ``null`` if the user cancelled rather than picking one.
    """
    return BrowseFolderResponse(path=browse_folder())


@router.put("/settings/sources", response_model=SourceConfigResponse)
def put_source_config_route(payload: SourceConfigUpdateRequest) -> SourceConfigResponse:
    """Update the source-scope configuration; a ``ConfigError`` maps to 500."""
    env = update_source_config(
        local_files_watch_dirs=payload.local_files_watch_dirs,
        notion_page_ids=payload.notion_page_ids,
    )
    return SourceConfigResponse(
        local_files_watch_dirs=[str(d) for d in env.watch_dirs],
        notion_page_ids=env.notion_page_ids_list,
    )
