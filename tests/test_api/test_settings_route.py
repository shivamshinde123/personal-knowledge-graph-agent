"""Tests for GET/PUT /api/settings.

Both routes are monkeypatched at the config/settings.py call boundary
(``get_settings``/``update_llm_config``) rather than exercising the real
config.yaml write path here — that path already has its own thorough,
real-file-based coverage in tests/test_config/test_settings.py. This
keeps these tests focused on request/response shape and guarantees they
can never touch the real config.yaml.
"""

from types import SimpleNamespace


def fake_settings(provider_mode="mixed", local_model="llama3:8b", cloud_model="x"):
    return SimpleNamespace(
        config=SimpleNamespace(
            llm=SimpleNamespace(
                provider_mode=provider_mode,
                local_model=local_model,
                cloud_model=cloud_model,
            )
        )
    )


class TestGetSettings:
    def test_returns_the_current_llm_config(self, client, monkeypatch):
        monkeypatch.setattr(
            "api.routes.settings.get_settings",
            lambda: fake_settings(
                provider_mode="mixed",
                local_model="llama3:8b",
                cloud_model="anthropic/claude-sonnet-4",
            ),
        )

        response = client.get("/api/settings")

        assert response.status_code == 200
        assert response.json() == {
            "provider_mode": "mixed",
            "local_model": "llama3:8b",
            "cloud_model": "anthropic/claude-sonnet-4",
        }


class TestPutSettings:
    def test_updates_and_returns_the_new_config(self, client, monkeypatch):
        captured = {}

        def fake_update(*, provider_mode=None, local_model=None, cloud_model=None):
            captured["provider_mode"] = provider_mode
            captured["cloud_model"] = cloud_model
            return SimpleNamespace(
                llm=SimpleNamespace(
                    provider_mode=provider_mode or "mixed",
                    local_model=local_model or "llama3:8b",
                    cloud_model=cloud_model or "anthropic/claude-sonnet-4",
                )
            )

        monkeypatch.setattr("api.routes.settings.update_llm_config", fake_update)

        response = client.put(
            "/api/settings",
            json={"provider_mode": "fully_cloud", "cloud_model": "openai/gpt-4o"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "updated"
        assert body["provider_mode"] == "fully_cloud"
        assert body["cloud_model"] == "openai/gpt-4o"
        assert captured["provider_mode"] == "fully_cloud"
        assert captured["cloud_model"] == "openai/gpt-4o"

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
