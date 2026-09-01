"""Tests for GET/PUT /api/settings.

Both routes are monkeypatched at the config/settings.py call boundary
(``get_settings``/``update_llm_config``) rather than exercising the real
config.yaml write path here — that path already has its own thorough,
real-file-based coverage in tests/test_config/test_settings.py. This
keeps these tests focused on request/response shape and guarantees they
can never touch the real config.yaml.
"""

from datetime import date
from types import SimpleNamespace


def fake_settings(
    provider_mode="fully_local",
    local_generation_model="llama3:8b",
    cloud_generation_model="x",
    cloud_embedding_model="openai/text-embedding-3-small",
):
    return SimpleNamespace(
        config=SimpleNamespace(
            llm=SimpleNamespace(
                provider_mode=provider_mode,
                local_generation_model=local_generation_model,
                cloud_generation_model=cloud_generation_model,
                cloud_embedding_model=cloud_embedding_model,
            )
        )
    )


class TestGetSettings:
    def test_returns_the_current_llm_config(self, client, monkeypatch):
        monkeypatch.setattr(
            "api.routes.settings.get_settings",
            lambda: fake_settings(
                provider_mode="fully_local",
                local_generation_model="llama3:8b",
                cloud_generation_model="anthropic/claude-sonnet-4",
                cloud_embedding_model="openai/text-embedding-3-small",
            ),
        )

        response = client.get("/api/settings")

        assert response.status_code == 200
        assert response.json() == {
            "provider_mode": "fully_local",
            "local_generation_model": "llama3:8b",
            "cloud_generation_model": "anthropic/claude-sonnet-4",
            "cloud_embedding_model": "openai/text-embedding-3-small",
        }


class TestPutSettings:
    def test_updates_and_returns_the_new_config(self, client, monkeypatch):
        captured = {}

        def fake_update(
            *,
            provider_mode=None,
            local_generation_model=None,
            cloud_generation_model=None,
            cloud_embedding_model=None,
        ):
            captured["provider_mode"] = provider_mode
            captured["cloud_generation_model"] = cloud_generation_model
            captured["cloud_embedding_model"] = cloud_embedding_model
            return SimpleNamespace(
                llm=SimpleNamespace(
                    provider_mode=provider_mode or "fully_local",
                    local_generation_model=local_generation_model or "llama3:8b",
                    cloud_generation_model=cloud_generation_model
                    or "anthropic/claude-sonnet-4",
                    cloud_embedding_model=cloud_embedding_model
                    or "openai/text-embedding-3-small",
                )
            )

        monkeypatch.setattr("api.routes.settings.update_llm_config", fake_update)

        response = client.put(
            "/api/settings",
            json={
                "provider_mode": "fully_cloud",
                "cloud_generation_model": "openai/gpt-4o",
                "cloud_embedding_model": "openai/text-embedding-3-large",
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "updated"
        assert body["provider_mode"] == "fully_cloud"
        assert body["cloud_generation_model"] == "openai/gpt-4o"
        assert body["cloud_embedding_model"] == "openai/text-embedding-3-large"
        assert captured["provider_mode"] == "fully_cloud"
        assert captured["cloud_generation_model"] == "openai/gpt-4o"
        assert captured["cloud_embedding_model"] == "openai/text-embedding-3-large"

    def test_invalid_provider_mode_returns_422(self, client):
        response = client.put(
            "/api/settings", json={"provider_mode": "not_a_real_mode"}
        )

        assert response.status_code == 422
        assert response.json()["error"] == "validation_error"

    def test_config_error_is_mapped_to_500(self, client, monkeypatch):
        from config.settings import ConfigError

        def boom(**kwargs):
            raise ConfigError("could not write config.yaml")

        monkeypatch.setattr("api.routes.settings.update_llm_config", boom)

        response = client.put("/api/settings", json={"provider_mode": "fully_cloud"})

        assert response.status_code == 500
        assert response.json()["error"] == "config_error"


def fake_env_settings(
    watch_dirs=(),
    notion_page_ids=(),
    github_repos=(),
    gmail_date_range_start=None,
    gmail_date_range_end=None,
    github_date_range_start=None,
    github_date_range_end=None,
    calendar_date_range_start=None,
    calendar_date_range_end=None,
):
    return SimpleNamespace(
        env=SimpleNamespace(
            watch_dirs=list(watch_dirs),
            notion_page_ids_list=list(notion_page_ids),
            github_repos_list=list(github_repos),
            # Named for what the route actually reads — see
            # api/routes/settings.py, DECISIONS.md: Gmail/Calendar always
            # carry an *effective* (defaulted-if-unset) value, unlike
            # GitHub's own date range.
            effective_gmail_date_range_start=gmail_date_range_start,
            effective_gmail_date_range_end=gmail_date_range_end,
            github_date_range_start=github_date_range_start,
            github_date_range_end=github_date_range_end,
            effective_calendar_date_range_start=calendar_date_range_start,
            effective_calendar_date_range_end=calendar_date_range_end,
        )
    )


class TestGetSourceConfig:
    def test_returns_the_current_source_scope(self, client, monkeypatch):
        monkeypatch.setattr(
            "api.routes.settings.get_settings",
            lambda: fake_env_settings(
                watch_dirs=["/a/b"],
                notion_page_ids=["page-1"],
                github_repos=["me/repo-a"],
                gmail_date_range_start=date(2026, 1, 1),
                gmail_date_range_end=date(2026, 6, 30),
                calendar_date_range_start=date(2026, 7, 1),
                calendar_date_range_end=date(2026, 7, 31),
            ),
        )

        response = client.get("/api/settings/sources")

        assert response.status_code == 200
        assert response.json() == {
            "local_files_watch_dirs": ["/a/b"],
            "notion_page_ids": ["page-1"],
            "github_repos": ["me/repo-a"],
            "gmail_date_range_start": "2026-01-01",
            "gmail_date_range_end": "2026-06-30",
            "github_date_range_start": None,
            "github_date_range_end": None,
            "calendar_date_range_start": "2026-07-01",
            "calendar_date_range_end": "2026-07-31",
        }


class TestPutSourceConfig:
    def test_updates_and_returns_the_new_scope(self, client, monkeypatch):
        captured = {}

        def fake_update(
            *,
            local_files_watch_dirs=None,
            notion_page_ids=None,
            github_repos=None,
            gmail_date_range_start=None,
            gmail_date_range_end=None,
            github_date_range_start=None,
            github_date_range_end=None,
            calendar_date_range_start=None,
            calendar_date_range_end=None,
        ):
            captured["local_files_watch_dirs"] = local_files_watch_dirs
            captured["notion_page_ids"] = notion_page_ids
            captured["github_repos"] = github_repos
            captured["gmail_date_range_start"] = gmail_date_range_start
            return SimpleNamespace(
                watch_dirs=local_files_watch_dirs or [],
                notion_page_ids_list=notion_page_ids or [],
                github_repos_list=github_repos or [],
                effective_gmail_date_range_start=(
                    date.fromisoformat(gmail_date_range_start)
                    if gmail_date_range_start
                    else date(2026, 1, 1)
                ),
                effective_gmail_date_range_end=(
                    date.fromisoformat(gmail_date_range_end)
                    if gmail_date_range_end
                    else date(2026, 1, 1)
                ),
                github_date_range_start=None,
                github_date_range_end=None,
                effective_calendar_date_range_start=(
                    date.fromisoformat(calendar_date_range_start)
                    if calendar_date_range_start
                    else date(2026, 1, 1)
                ),
                effective_calendar_date_range_end=(
                    date.fromisoformat(calendar_date_range_end)
                    if calendar_date_range_end
                    else date(2026, 1, 1)
                ),
            )

        monkeypatch.setattr("api.routes.settings.update_source_config", fake_update)

        response = client.put(
            "/api/settings/sources",
            json={
                "local_files_watch_dirs": ["/a/b"],
                "notion_page_ids": ["page-1", "page-2"],
                "github_repos": ["me/repo-a"],
                "gmail_date_range_start": "2026-01-01",
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["local_files_watch_dirs"] == ["/a/b"]
        assert body["notion_page_ids"] == ["page-1", "page-2"]
        assert body["github_repos"] == ["me/repo-a"]
        assert body["gmail_date_range_start"] == "2026-01-01"
        assert captured["local_files_watch_dirs"] == ["/a/b"]
        assert captured["notion_page_ids"] == ["page-1", "page-2"]
        assert captured["github_repos"] == ["me/repo-a"]
        assert captured["gmail_date_range_start"] == "2026-01-01"

    def test_calendar_date_range_passes_through(self, client, monkeypatch):
        captured = {}

        def fake_update(*, calendar_date_range_start=None, **_kwargs):
            captured["calendar_date_range_start"] = calendar_date_range_start
            return SimpleNamespace(
                watch_dirs=[],
                notion_page_ids_list=[],
                github_repos_list=[],
                effective_gmail_date_range_start=date(2026, 1, 1),
                effective_gmail_date_range_end=date(2026, 1, 1),
                github_date_range_start=None,
                github_date_range_end=None,
                effective_calendar_date_range_start=date.fromisoformat(
                    calendar_date_range_start
                ),
                effective_calendar_date_range_end=date(2026, 8, 1),
            )

        monkeypatch.setattr("api.routes.settings.update_source_config", fake_update)

        response = client.put(
            "/api/settings/sources",
            json={"calendar_date_range_start": "2026-07-01"},
        )

        assert response.status_code == 200
        assert response.json()["calendar_date_range_start"] == "2026-07-01"
        assert captured["calendar_date_range_start"] == "2026-07-01"

    def test_config_error_is_mapped_to_500(self, client, monkeypatch):
        from config.settings import ConfigError

        def boom(**kwargs):
            raise ConfigError("could not write .env")

        monkeypatch.setattr("api.routes.settings.update_source_config", boom)

        response = client.put(
            "/api/settings/sources", json={"local_files_watch_dirs": ["/a"]}
        )

        assert response.status_code == 500
        assert response.json()["error"] == "config_error"


class TestBrowseFolder:
    def test_returns_the_selected_path(self, client, monkeypatch):
        monkeypatch.setattr(
            "api.routes.settings.browse_folder",
            lambda: "C:\\Users\\you\\Documents\\Notes",
        )

        response = client.post("/api/settings/browse-folder")

        assert response.status_code == 200
        assert response.json() == {"path": "C:\\Users\\you\\Documents\\Notes"}

    def test_returns_null_path_when_the_dialog_is_cancelled(self, client, monkeypatch):
        monkeypatch.setattr("api.routes.settings.browse_folder", lambda: None)

        response = client.post("/api/settings/browse-folder")

        assert response.status_code == 200
        assert response.json() == {"path": None}
