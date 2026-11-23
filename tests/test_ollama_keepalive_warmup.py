"""Удержание модели Ollama: keep_alive и прогрев (DECISIONS #25, RUNBOOK 8.7)."""

from __future__ import annotations

import json
import urllib.error

import pytest

from onec_analyst.llm_agent import (
    LLMSettings,
    OllamaChatClient,
    settings_from_env,
    warmup_ollama,
)

BASE_ENV = {"LLM_PROVIDER": "ollama", "OLLAMA_ENABLED": "true"}


def _ollama_settings(**overrides: object) -> LLMSettings:
    base: dict[str, object] = {
        "provider": "ollama",
        "model": "qwen3-4b-instruct-2507",
        "api_key": "ollama",
        "base_url": "http://localhost:11434/v1",
    }
    base.update(overrides)
    return LLMSettings(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# settings_from_env: чтение LLM_KEEP_ALIVE / LLM_KEEP_ALIVE_OLLAMA
# --------------------------------------------------------------------------- #
def test_keep_alive_default_is_empty() -> None:
    assert settings_from_env(BASE_ENV, provider="ollama").keep_alive == ""


def test_keep_alive_provider_scoped_variable_is_read() -> None:
    settings = settings_from_env(
        {**BASE_ENV, "LLM_KEEP_ALIVE_OLLAMA": "30m"}, provider="ollama"
    )

    assert settings.keep_alive == "30m"


def test_keep_alive_generic_applies_to_default_provider() -> None:
    settings = settings_from_env(
        {"LLM_PROVIDER": "ollama", "LLM_KEEP_ALIVE": "10m"}, provider="ollama"
    )

    assert settings.keep_alive == "10m"


def test_keep_alive_specific_beats_generic() -> None:
    settings = settings_from_env(
        {
            "LLM_PROVIDER": "deepseek",
            "LLM_KEEP_ALIVE": "10m",
            "LLM_KEEP_ALIVE_OLLAMA": "30m",
        },
        provider="ollama",
    )

    assert settings.keep_alive == "30m"


def test_keep_alive_generic_does_not_leak_to_other_provider() -> None:
    """``LLM_KEEP_ALIVE`` — сокращение только для провайдера из LLM_PROVIDER."""
    settings = settings_from_env(
        {"LLM_PROVIDER": "deepseek", "LLM_KEEP_ALIVE": "10m"}, provider="ollama"
    )

    assert settings.keep_alive == ""


# --------------------------------------------------------------------------- #
# keep_alive попадает только в корень payload /api/chat (ollama)
# --------------------------------------------------------------------------- #
def test_keep_alive_goes_to_payload_root_not_options() -> None:
    client = OllamaChatClient(_ollama_settings(keep_alive="30m"))

    payload = client._payload([{"role": "user", "content": "x"}])

    assert payload["keep_alive"] == "30m"
    assert "keep_alive" not in payload["options"]


def test_keep_alive_omitted_when_empty() -> None:
    client = OllamaChatClient(_ollama_settings(keep_alive=""))

    payload = client._payload([{"role": "user", "content": "x"}])

    assert "keep_alive" not in payload


# --------------------------------------------------------------------------- #
# warmup_ollama: /api/generate без prompt, никогда не бросает исключение
# --------------------------------------------------------------------------- #
def test_warmup_sends_generate_without_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class _FakeResponse:
        def read(self) -> bytes:
            return b"{}"

        def __enter__(self) -> _FakeResponse:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def _fake_urlopen(request: object, timeout: object = None) -> _FakeResponse:
        captured["url"] = request.full_url
        captured["data"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr("onec_analyst.llm_agent.urllib.request.urlopen", _fake_urlopen)
    settings = _ollama_settings(keep_alive="30m")

    assert warmup_ollama(settings) is True
    assert captured["url"] == "http://localhost:11434/api/generate"
    assert captured["data"] == {"model": "qwen3-4b-instruct-2507", "keep_alive": "30m"}
    assert "prompt" not in captured["data"]
    assert captured["timeout"] == 120


def test_warmup_defaults_keep_alive_to_30m(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class _FakeResponse:
        def read(self) -> bytes:
            return b"{}"

        def __enter__(self) -> _FakeResponse:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def _fake_urlopen(request: object, timeout: object = None) -> _FakeResponse:
        captured["data"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse()

    monkeypatch.setattr("onec_analyst.llm_agent.urllib.request.urlopen", _fake_urlopen)

    assert warmup_ollama(_ollama_settings(keep_alive="")) is True
    assert captured["data"]["keep_alive"] == "30m"


def test_warmup_network_error_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*args: object, **kwargs: object) -> None:
        raise urllib.error.URLError("нет сети")

    monkeypatch.setattr("onec_analyst.llm_agent.urllib.request.urlopen", _raise)

    assert warmup_ollama(_ollama_settings(keep_alive="30m")) is False


def test_warmup_skips_non_ollama() -> None:
    settings = LLMSettings(
        provider="deepseek",
        model="deepseek-chat",
        api_key="k",
        base_url="https://api.deepseek.com/v1",
    )

    assert warmup_ollama(settings) is False


# --------------------------------------------------------------------------- #
# app: opt-in флаг OLLAMA_WARMUP
# --------------------------------------------------------------------------- #
def test_app_warmup_flag_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import _ollama_warmup_enabled

    monkeypatch.setenv("OLLAMA_WARMUP", "1")
    assert _ollama_warmup_enabled() is True

    monkeypatch.setenv("OLLAMA_WARMUP", "0")
    assert _ollama_warmup_enabled() is False

    monkeypatch.delenv("OLLAMA_WARMUP", raising=False)
    assert _ollama_warmup_enabled() is False
