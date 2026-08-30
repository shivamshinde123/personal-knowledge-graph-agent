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

from api.schemas import (
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
        local_model=llm.local_model,
        cloud_model=llm.cloud_model,
    )


@router.put("/settings", response_model=SettingsUpdateResponse)
def put_settings_route(payload: SettingsUpdateRequest) -> SettingsUpdateResponse:
    """Update the LLM provider configuration; a ``ConfigError`` maps to 500."""
    config = update_llm_config(
        provider_mode=payload.provider_mode,
        local_model=payload.local_model,
        cloud_model=payload.cloud_model,
    )
    return SettingsUpdateResponse(
        status="updated",
        provider_mode=config.llm.provider_mode,
        local_model=config.llm.local_model,
        cloud_model=config.llm.cloud_model,
    )


@router.get("/settings/sources", response_model=SourceConfigResponse)
def get_source_config_route() -> SourceConfigResponse:
    """Return the current source-scope configuration (watch folders, Notion scope)."""
    env = get_settings().env
    return SourceConfigResponse(
        local_files_watch_dirs=[str(d) for d in env.watch_dirs],
        notion_page_ids=env.notion_page_ids_list,
    )


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
