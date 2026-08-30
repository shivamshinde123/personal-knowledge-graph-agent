"""Tests for GET/PUT /api/settings.

Both routes are monkeypatched at the config/settings.py call boundary
(``get_settings``/``update_llm_config``) rather than exercising the real
config.yaml write path here — that path already has its own thorough,
real-file-based coverage in tests/test_config/test_settings.py. This
keeps these tests focused on request/response shape and guarantees they
can never touch the real config.yaml.
"""

from types import SimpleNamespace


def fake_settings(
    provider_mode="fully_local",
    local_generation_model="llama3:8b",
    cloud_generation_model="x",
):
    return SimpleNamespace(
        config=SimpleNamespace(
            llm=SimpleNamespace(
                provider_mode=provider_mode,
                local_generation_model=local_generation_model,
                cloud_generation_model=cloud_generation_model,
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
            ),
        )
        monkeypatch.setattr(
            "api.routes.settings.get_embedding_model_names",
            lambda: (
                "sentence-transformers/all-MiniLM-L6-v2",
                "openai/text-embedding-3-small",
            ),
        )

        response = client.get("/api/settings")

        assert response.status_code == 200
        assert response.json() == {
            "provider_mode": "fully_local",
            "local_generation_model": "llama3:8b",
            "local_embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
            "cloud_generation_model": "anthropic/claude-sonnet-4",
            "cloud_embedding_model": "openai/text-embedding-3-small",
        }

    def test_embedding_models_are_not_settable(self, client, monkeypatch):
        """A supplied embedding-model value is silently ignored.

        The schema has no field for it, so it never reaches
        update_llm_config, and the response always reports the frozen
        constant instead.
        """
        monkeypatch.setattr(
            "api.routes.settings.update_llm_config",
            lambda **kwargs: fake_settings().config,
        )
        monkeypatch.setattr(
            "api.routes.settings.get_embedding_model_names",
            lambda: (
                "sentence-transformers/all-MiniLM-L6-v2",
                "openai/text-embedding-3-small",
            ),
        )

        response = client.put(
            "/api/settings",
            json={"local_embedding_model": "some-other-model"},
        )

        assert response.status_code == 200
        assert (
            response.json()["local_embedding_model"]
            == "sentence-transformers/all-MiniLM-L6-v2"
        )


class TestPutSettings:
    def test_updates_and_returns_the_new_config(self, client, monkeypatch):
        captured = {}

        def fake_update(
            *,
            provider_mode=None,
            local_generation_model=None,
            cloud_generation_model=None,
        ):
            captured["provider_mode"] = provider_mode
            captured["cloud_generation_model"] = cloud_generation_model
            return SimpleNamespace(
                llm=SimpleNamespace(
                    provider_mode=provider_mode or "fully_local",
                    local_generation_model=local_generation_model or "llama3:8b",
                    cloud_generation_model=cloud_generation_model
                    or "anthropic/claude-sonnet-4",
                )
            )

        monkeypatch.setattr(
            "api.routes.settings.get_embedding_model_names",
            lambda: (
                "sentence-transformers/all-MiniLM-L6-v2",
                "openai/text-embedding-3-small",
            ),
        )

        monkeypatch.setattr("api.routes.settings.update_llm_config", fake_update)

        response = client.put(
            "/api/settings",
            json={
                "provider_mode": "fully_cloud",
                "cloud_generation_model": "openai/gpt-4o",
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "updated"
        assert body["provider_mode"] == "fully_cloud"
        assert body["cloud_generation_model"] == "openai/gpt-4o"
        assert captured["provider_mode"] == "fully_cloud"
        assert captured["cloud_generation_model"] == "openai/gpt-4o"

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


def fake_env_settings(watch_dirs=(), notion_page_ids=()):
    return SimpleNamespace(
        env=SimpleNamespace(
            watch_dirs=list(watch_dirs), notion_page_ids_list=list(notion_page_ids)
        )
    )


class TestGetSourceConfig:
    def test_returns_the_current_source_scope(self, client, monkeypatch):
        monkeypatch.setattr(
            "api.routes.settings.get_settings",
            lambda: fake_env_settings(watch_dirs=["/a/b"], notion_page_ids=["page-1"]),
        )

        response = client.get("/api/settings/sources")

        assert response.status_code == 200
        assert response.json() == {
            "local_files_watch_dirs": ["/a/b"],
            "notion_page_ids": ["page-1"],
        }


class TestPutSourceConfig:
    def test_updates_and_returns_the_new_scope(self, client, monkeypatch):
        captured = {}

        def fake_update(*, local_files_watch_dirs=None, notion_page_ids=None):
            captured["local_files_watch_dirs"] = local_files_watch_dirs
            captured["notion_page_ids"] = notion_page_ids
            return SimpleNamespace(
                watch_dirs=local_files_watch_dirs or [],
                notion_page_ids_list=notion_page_ids or [],
            )

        monkeypatch.setattr("api.routes.settings.update_source_config", fake_update)

        response = client.put(
            "/api/settings/sources",
            json={
                "local_files_watch_dirs": ["/a/b"],
                "notion_page_ids": ["page-1", "page-2"],
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["local_files_watch_dirs"] == ["/a/b"]
        assert body["notion_page_ids"] == ["page-1", "page-2"]
        assert captured["local_files_watch_dirs"] == ["/a/b"]
        assert captured["notion_page_ids"] == ["page-1", "page-2"]

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
