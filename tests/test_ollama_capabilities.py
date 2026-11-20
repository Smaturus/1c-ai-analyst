"""Возможности модели Ollama: проба ``/api/show`` и согласование think/tools."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from onec_analyst.llm_agent import (
    LLMError,
    LLMSettings,
    ollama_capabilities,
    with_ollama_capabilities,
)


class _FakeResponse:
    """Минимальный ответ ``urlopen``."""

    def __init__(self, payload: object) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> bool:
        return False


def _settings(**overrides: object) -> LLMSettings:
    base: dict[str, object] = {
        "provider": "ollama",
        "model": "qwen3:4b",
        "api_key": "ollama",
        "base_url": "http://localhost:11434/v1",
    }
    base.update(overrides)
    return LLMSettings(**base)  # type: ignore[arg-type]


def test_capabilities_are_read_from_api_show(monkeypatch: pytest.MonkeyPatch) -> None:
    """``/api/show`` вызывается на корне API, модель — в теле запроса."""
    seen: dict[str, object] = {}

    def urlopen(request: object, timeout: object = None) -> _FakeResponse:
        seen["url"] = getattr(request, "full_url", "")
        seen["body"] = json.loads(getattr(request, "data", b"{}"))
        return _FakeResponse({"capabilities": ["completion", "tools", "thinking"]})

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    capabilities = ollama_capabilities(_settings())

    assert capabilities == frozenset({"completion", "tools", "thinking"})
    assert seen["url"] == "http://localhost:11434/api/show"
    assert seen["body"] == {"model": "qwen3:4b"}


def test_capabilities_empty_for_other_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Для не-ollama провайдера проба не делается вовсе."""
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *args, **kwargs: pytest.fail("сеть не нужна")
    )

    capabilities = ollama_capabilities(
        LLMSettings(provider="deepseek", model="deepseek-chat", api_key="k")
    )

    assert capabilities == frozenset()


def test_capabilities_failure_raises_llm_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Сервер недоступен — ``LLMError``, а не сырое исключение."""

    def urlopen(request: object, timeout: object = None) -> object:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    with pytest.raises(LLMError):
        ollama_capabilities(_settings())


def test_capabilities_missing_field_gives_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ответ без ``capabilities`` (старая версия) — пустой набор."""
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *args, **kwargs: _FakeResponse({"model_info": {}}),
    )

    assert ollama_capabilities(_settings()) == frozenset()


def test_qwen_capabilities_keep_think_and_tools() -> None:
    """qwen3 умеет и thinking, и tools — настройки не меняются."""
    settings = _settings(think=True, tools=True)

    resolved = with_ollama_capabilities(
        settings, {"completion", "tools", "thinking"}
    )

    assert resolved.think is True
    assert resolved.tools is True


def test_gemma_capabilities_disable_think_and_tools() -> None:
    """gemma3 без thinking/tools: флаги выключаются, остальное не трогается."""
    settings = _settings(model="gemma3:4b", think=True, tools=True)

    resolved = with_ollama_capabilities(settings, {"completion", "vision"})

    assert resolved.think is False
    assert resolved.tools is False
    assert resolved.model == "gemma3:4b"


def test_explicit_off_stays_off() -> None:
    """Явное выключение не включается обратно возможностями модели."""
    settings = _settings(think=False, tools=False)

    resolved = with_ollama_capabilities(
        settings, {"completion", "tools", "thinking"}
    )

    assert resolved.think is False
    assert resolved.tools is False


def test_empty_capabilities_keep_settings() -> None:
    """Сбой пробы не ломает рабочий профиль: настройки как были."""
    settings = _settings(think=True, tools=True)

    assert with_ollama_capabilities(settings, frozenset()) is settings


def test_other_provider_settings_untouched() -> None:
    """DeepSeek не затрагивается."""
    settings = LLMSettings(provider="deepseek", model="deepseek-chat", api_key="k")

    assert with_ollama_capabilities(settings, {"completion"}) is settings
