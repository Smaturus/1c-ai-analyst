"""Переключатель нативных tools для Ollama (DECISIONS #23).

Импортированные GGUF-модели в Ollama без ``--jinja`` не умеют tools: сервер
отвечает 500. ``OLLAMA_TOOLS=0`` переключает клиент на JSON-адаптер, не меняя
поведение остальных провайдеров.
"""

from __future__ import annotations

from onec_analyst.llm_agent import (
    LLMSettings,
    OllamaChatClient,
    make_client,
    settings_from_env,
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


def test_env_tools_default_on() -> None:
    """По умолчанию tools включены — поведение qwen3:4b не меняется."""
    assert settings_from_env(BASE_ENV, provider="ollama").tools is True


def test_env_tools_can_be_disabled() -> None:
    """OLLAMA_TOOLS=0/false выключает нативные tools."""
    for value in ("0", "false", "off", "no"):
        settings = settings_from_env(
            {**BASE_ENV, "OLLAMA_TOOLS": value}, provider="ollama"
        )
        assert settings.tools is False, value


def test_client_without_tools_reports_no_tool_support() -> None:
    """Клиент без tools заставит агента идти JSON-адаптером."""
    client = OllamaChatClient(_ollama_settings(tools=False))

    assert client.supports_tools is False


def test_client_with_tools_keeps_native_mode() -> None:
    """Обычная модель (qwen3:4b) по-прежнему объявляет tools."""
    client = OllamaChatClient(_ollama_settings(model="qwen3:4b"))

    assert client.supports_tools is True


def test_other_provider_tools_unaffected() -> None:
    """DeepSeek не затрагивается: tools остаются включены."""
    settings = LLMSettings(
        provider="deepseek",
        model="deepseek-chat",
        api_key="k",
        base_url="https://api.deepseek.com/v1",
    )
    client = make_client(settings)

    assert settings.tools is True
    assert getattr(client, "supports_tools", False) is True


def test_payload_includes_temperature_and_num_ctx() -> None:
    """``LLM_TEMPERATURE=0`` доезжает до /api/chat; ``num_ctx`` не теряется.

    Без явной ``temperature`` Ollama берёт дефолт модели (у gemma3 — 1.0),
    и ноль из ``.env`` молча игнорируется.
    """
    client = OllamaChatClient(_ollama_settings(num_ctx=8192, temperature=0.0))

    payload = client._payload([{"role": "user", "content": "x"}])

    assert payload["options"]["temperature"] == 0.0
    assert payload["options"]["num_ctx"] == 8192


def test_payload_omits_num_ctx_when_unset() -> None:
    """Без ``num_ctx`` ключа нет: ``options`` несёт только температуру."""
    client = OllamaChatClient(_ollama_settings(temperature=0.0))

    payload = client._payload([{"role": "user", "content": "x"}])

    assert payload["options"] == {"temperature": 0.0}
