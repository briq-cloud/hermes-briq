from __future__ import annotations

import importlib

import pytest
from fakes import FakeBriq, ctx
from hermes_briq import register

provider_module = importlib.import_module("hermes_briq.provider")
environment_module = importlib.import_module("hermes_briq.environment")


@pytest.fixture
def provider() -> provider_module.BriqProvider:
    return provider_module.BriqProvider()


def test_register_hands_the_provider_to_hermes() -> None:
    context = ctx()
    register(context)
    assert [type(p).__name__ for p in context.registered] == ["BriqProvider"]
    assert context.registered[0].name == "briq"


def test_the_provider_declares_what_core_classifies_by(provider) -> None:
    assert provider.is_remote and provider.is_container
    assert provider.default_cwd == "~"
    assert provider.cache_path_base == "~/.hermes"
    assert provider.strip_env_keys == frozenset({"BRIQ_API_KEY"})
    assert not provider.supports_streams


def test_unavailable_without_a_key_and_ready_with_one(provider, monkeypatch) -> None:
    assert not provider.is_available()
    assert provider.probe()[0] == "needs_setup"
    monkeypatch.setenv("BRIQ_API_KEY", "bk_test")
    assert provider.is_available()
    assert provider.probe() == ("ready", "")


def test_availability_makes_no_client(provider, monkeypatch) -> None:
    monkeypatch.setenv("BRIQ_API_KEY", "bk_test")

    def refuse(*args, **kwargs):
        raise AssertionError("a check must not open a client")

    monkeypatch.setattr(environment_module, "new_client", refuse)
    assert provider.is_available() and provider.probe()[0] == "ready"
    provider.doctor_checks()
    provider.setup_instructions()


def test_requirements_reject_a_size_briq_does_not_have(provider, monkeypatch) -> None:
    monkeypatch.setenv("BRIQ_API_KEY", "bk_test")
    assert provider.check_requirements({})
    monkeypatch.setenv("TERMINAL_BRIQ_SIZE", "huge")
    assert not provider.check_requirements({})


def test_the_key_never_shows_in_setup_text(provider, monkeypatch) -> None:
    monkeypatch.setenv("BRIQ_API_KEY", "bk_secret_value")
    text = " ".join(provider.setup_instructions() + [d for _, _, d in provider.doctor_checks()])
    assert "bk_secret_value" not in text


def test_create_environment_passes_the_settings(provider, monkeypatch) -> None:
    monkeypatch.setenv("BRIQ_API_KEY", "bk_test")
    monkeypatch.setenv("BRIQ_API_URL", "http://localhost:3001")
    monkeypatch.setenv("TERMINAL_BRIQ_SIZE", "large")
    monkeypatch.setenv("TERMINAL_BRIQ_IMAGE", "ghcr.io/acme/box:1")
    monkeypatch.setenv("TERMINAL_BRIQ_SLEEP", "30m")
    seen: dict = {}
    client = FakeBriq()

    def new_client(key, base_url):
        seen.update(key=key, base_url=base_url)
        return client

    monkeypatch.setattr(environment_module, "new_client", new_client)
    provider.create_environment(
        cwd="~", timeout=60, task_id="t1", container_config={"container_persistent": False}
    )
    assert seen == {"key": "bk_test", "base_url": "http://localhost:3001"}
    assert client.created == [
        {
            "name": client.created[0]["name"],
            "size": "large",
            "image": "ghcr.io/acme/box:1",
            "sleep": "30m",
            "idempotency_key": client.created[0]["idempotency_key"],
        }
    ]


def test_a_per_task_image_wins_over_the_setting(provider, monkeypatch) -> None:
    monkeypatch.setenv("BRIQ_API_KEY", "bk_test")
    monkeypatch.setenv("TERMINAL_BRIQ_IMAGE", "ghcr.io/acme/box:1")
    client = FakeBriq()
    monkeypatch.setattr(environment_module, "new_client", lambda key, url: client)
    provider.create_environment(cwd="~", timeout=60, image="python:3.12", task_id="t")
    assert client.created[0]["image"] == "python:3.12"


def test_create_environment_refuses_a_bad_size_before_any_request(provider, monkeypatch) -> None:
    monkeypatch.setenv("BRIQ_API_KEY", "bk_test")
    monkeypatch.setenv("TERMINAL_BRIQ_SIZE", "tiny")
    monkeypatch.setattr(environment_module, "new_client", lambda *a: pytest.fail("no client"))
    with pytest.raises(ValueError, match="small, medium, large, xlarge"):
        provider.create_environment(cwd="~", timeout=60)
