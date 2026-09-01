"""``GET``/``PUT /api/settings``: the current LLM provider configuration.

Per ``docs/API_Specification.docx`` sections 3.6/3.7. Reads/writes
``config/settings.py`` directly rather than via an ``agent/`` module —
``api/__init__.py``'s "never reaching into storage or providers directly"
rule names those two layers specifically; ``config`` is the shared
configuration layer every other layer (including ``agent/``) already
depends on directly, not a layer this rule is about. See ``DECISIONS.md``.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter
from fastapi.responses import JSONResponse

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
        local_embedding_model=llm.local_embedding_model,
        cloud_generation_model=llm.cloud_generation_model,
        cloud_embedding_model=llm.cloud_embedding_model,
    )


@router.put("/settings", response_model=SettingsUpdateResponse)
def put_settings_route(payload: SettingsUpdateRequest) -> SettingsUpdateResponse:
    """Update the LLM provider configuration; a ``ConfigError`` maps to 500.

    Neither a ``provider_mode`` switch nor a ``cloud_embedding_model``/
    ``local_embedding_model`` change is specially handled here — the
    frontend is responsible for confirming with the user (double-confirm
    for ``provider_mode``) and triggering the appropriate reset afterward
    (see ``SettingsResponse``'s docstring).
    """
    config = update_llm_config(
        provider_mode=payload.provider_mode,
        local_generation_model=payload.local_generation_model,
        local_embedding_model=payload.local_embedding_model,
        cloud_generation_model=payload.cloud_generation_model,
        cloud_embedding_model=payload.cloud_embedding_model,
    )
    return SettingsUpdateResponse(
        status="updated",
        provider_mode=config.llm.provider_mode,
        local_generation_model=config.llm.local_generation_model,
        local_embedding_model=config.llm.local_embedding_model,
        cloud_generation_model=config.llm.cloud_generation_model,
        cloud_embedding_model=config.llm.cloud_embedding_model,
    )


@router.get("/settings/sources", response_model=SourceConfigResponse)
def get_source_config_route() -> SourceConfigResponse:
    """Return the current source-scope configuration.

    Watch folders, Notion scope, GitHub repo scope, and the Gmail/GitHub/
    Calendar date ranges. Gmail's and Calendar's date-range fields carry
    their *effective* (defaulted-if-unset) value, not the raw configured
    one — see ``SourceConfigResponse``'s docstring.
    """
    env = get_settings().env
    return SourceConfigResponse(
        local_files_watch_dirs=[str(d) for d in env.watch_dirs],
        notion_page_ids=env.notion_page_ids_list,
        github_repos=env.github_repos_list,
        gmail_date_range_start=_isoformat(env.effective_gmail_date_range_start),
        gmail_date_range_end=_isoformat(env.effective_gmail_date_range_end),
        github_date_range_start=_isoformat(env.github_date_range_start),
        github_date_range_end=_isoformat(env.github_date_range_end),
        calendar_date_range_start=_isoformat(env.effective_calendar_date_range_start),
        calendar_date_range_end=_isoformat(env.effective_calendar_date_range_end),
        running_in_docker=env.running_in_docker,
    )


@router.post("/settings/browse-folder", response_model=BrowseFolderResponse)
def browse_folder_route():
    """Open a native folder-picker dialog on the server machine.

    Only meaningful because this backend runs on the same local machine as
    the user, and only possible on Windows (``agent/browse.py`` shells out
    to PowerShell) — neither holds inside a Docker container, so this
    returns a clear ``422`` instead of attempting the dialog at all when
    ``running_in_docker`` is set (see ``EnvSettings.running_in_docker``'s
    docstring, ``DECISIONS.md``). Blocks until the dialog is closed
    otherwise; ``path`` is ``null`` if the user cancelled rather than
    picking one.
    """
    if get_settings().env.running_in_docker:
        return JSONResponse(
            status_code=422,
            content={
                "error": "not_available_in_docker",
                "detail": (
                    "The folder picker isn't available when running in "
                    "Docker. Set HOST_WATCH_DIR in your docker-compose "
                    ".env and restart the stack to change watched folders."
                ),
            },
        )
    return BrowseFolderResponse(path=browse_folder())


@router.put("/settings/sources", response_model=SourceConfigResponse)
def put_source_config_route(payload: SourceConfigUpdateRequest):
    """Update the source-scope configuration; a ``ConfigError`` maps to 500.

    ``local_files_watch_dirs`` is rejected with a ``422`` while
    ``running_in_docker`` is set — it's a fixed, volume-backed container
    path in that mode, not something the running app can change on its own
    (see ``SourceConfigResponse``'s docstring). Every other field is
    unaffected by this check.
    """
    if (
        payload.local_files_watch_dirs is not None
        and get_settings().env.running_in_docker
    ):
        return JSONResponse(
            status_code=422,
            content={
                "error": "not_available_in_docker",
                "detail": (
                    "local_files_watch_dirs can't be changed while running "
                    "in Docker. Set HOST_WATCH_DIR in your docker-compose "
                    ".env and restart the stack instead."
                ),
            },
        )
    env = update_source_config(
        local_files_watch_dirs=payload.local_files_watch_dirs,
        notion_page_ids=payload.notion_page_ids,
        github_repos=payload.github_repos,
        gmail_date_range_start=payload.gmail_date_range_start,
        gmail_date_range_end=payload.gmail_date_range_end,
        github_date_range_start=payload.github_date_range_start,
        github_date_range_end=payload.github_date_range_end,
        calendar_date_range_start=payload.calendar_date_range_start,
        calendar_date_range_end=payload.calendar_date_range_end,
    )
    return SourceConfigResponse(
        local_files_watch_dirs=[str(d) for d in env.watch_dirs],
        notion_page_ids=env.notion_page_ids_list,
        github_repos=env.github_repos_list,
        gmail_date_range_start=_isoformat(env.effective_gmail_date_range_start),
        gmail_date_range_end=_isoformat(env.effective_gmail_date_range_end),
        github_date_range_start=_isoformat(env.github_date_range_start),
        github_date_range_end=_isoformat(env.github_date_range_end),
        calendar_date_range_start=_isoformat(env.effective_calendar_date_range_start),
        calendar_date_range_end=_isoformat(env.effective_calendar_date_range_end),
        running_in_docker=env.running_in_docker,
    )


def _isoformat(value: date | None) -> str | None:
    """``date | None`` -> ``"YYYY-MM-DD" | None``, for ``SourceConfigResponse``."""
    return value.isoformat() if value is not None else None
