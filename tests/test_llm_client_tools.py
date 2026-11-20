"""Негативные сценарии fallback на tools в ``LLMClient`` (ТЗ 6.4 §4.2).

Проверяют, что JSON-fallback срабатывает только при явном маркере
неподдержки tools (статус 400/404/422 + маркер), а реальные ошибки —
неверная модель, 500, сеть/таймаут, ошибка схемы — пробрасываются наружу
как ``LLMError`` без «тихой» подмены ответа.

Сетевые вызовы подменяются через ``monkeypatch`` на ``_post_json``.
"""

from __future__ import annotations

from typing import Any

import pytest

from onec_analyst.llm_agent import (
    CompletionResult,
    LLMClient,
    LLMError,
    LLMSettings,
    ToolCall,
    build_tools,
)


def _client() -> LLMClient:
    return LLMClient(LLMSettings(provider="deepseek", api_key="k", base_url="http://x/v1"))


def _tool_call(name: str, arguments: str) -> dict[str, Any]:
    return {"function": {"name": name, "arguments": arguments}}


def _body(message: dict[str, Any]) -> dict[str, Any]:
    return {"choices": [{"message": message}]}


def test_400_invalid_model_raises_no_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """400 «invalid model» — не fallback: наружу LLMError, второй вызов не делается."""
    client = _client()
    calls: list[dict[str, Any]] = []

    def fake_post(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(payload)
        raise LLMError("HTTP 400: invalid model")

    monkeypatch.setattr(client, "_post_json", fake_post)
    with pytest.raises(LLMError):
        client.complete([{"role": "user", "content": "?"}], tools=build_tools())

    assert len(calls) == 1, "fallback не должен вызываться на «invalid model»"


def test_500_unknown_function_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """500 «unknown function» содержит слово function, но статус 500 → LLMError."""
    client = _client()
    calls: list[dict[str, Any]] = []

    def fake_post(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(payload)
        raise LLMError("HTTP 500: unknown function")

    monkeypatch.setattr(client, "_post_json", fake_post)
    with pytest.raises(LLMError):
        client.complete([{"role": "user", "content": "?"}], tools=build_tools())

    assert len(calls) == 1


@pytest.mark.parametrize(
    "message",
    [
        "Сеть недоступна: <urlopen error timed out>",
        "Сеть недоступна: [Errno 11001] getaddrinfo failed",
    ],
)
def test_network_and_timeout_raise(monkeypatch: pytest.MonkeyPatch, message: str) -> None:
    """Сетевая ошибка/таймаут без HTTP-статуса → LLMError, без fallback."""
    client = _client()
    calls: list[dict[str, Any]] = []

    def fake_post(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(payload)
        raise LLMError(message)

    monkeypatch.setattr(client, "_post_json", fake_post)
    with pytest.raises(LLMError):
        client.complete([{"role": "user", "content": "?"}], tools=build_tools())

    assert len(calls) == 1


def test_422_schema_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """422 про схему («Type properties… is wrong») — не fallback, а LLMError."""
    client = _client()
    calls: list[dict[str, Any]] = []

    def fake_post(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(payload)
        raise LLMError("HTTP 422: Type properties is wrong for function")

    monkeypatch.setattr(client, "_post_json", fake_post)
    with pytest.raises(LLMError):
        client.complete([{"role": "user", "content": "?"}], tools=build_tools())

    assert len(calls) == 1


def test_400_tools_not_supported_falls_back_to_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """400 с явным маркером «tools are not supported» → JSON-fallback."""
    client = _client()

    def fake_post(payload: dict[str, Any]) -> dict[str, Any]:
        if "tools" in payload:
            raise LLMError("HTTP 400: tools are not supported")
        return _body({"content": '{"intent":"total","params":{}}'})

    monkeypatch.setattr(client, "_post_json", fake_post)
    result = client.complete([{"role": "user", "content": "?"}], tools=build_tools())

    assert result.content == '{"intent":"total","params":{}}'
    assert result.tool_calls == ()


@pytest.mark.parametrize(
    "marker",
    [
        "tool calling is not supported",
        "function calling is not supported",
        "does not support tools",
        "does not support function",
        "unknown field 'tools'",
        'unknown field "tools"',
        "unrecognized request argument: tools",
        "unrecognized request argument supplied: tools",
    ],
)
def test_unsupported_markers_trigger_fallback(
    monkeypatch: pytest.MonkeyPatch, marker: str
) -> None:
    """Каждый маркер неподдержки tools разрешает JSON-fallback."""
    client = _client()

    def fake_post(payload: dict[str, Any]) -> dict[str, Any]:
        if "tools" in payload:
            raise LLMError(f"HTTP 400: {marker}")
        return _body({"content": '{"intent":"total","params":{}}'})

    monkeypatch.setattr(client, "_post_json", fake_post)
    result = client.complete([{"role": "user", "content": "?"}], tools=build_tools())

    assert result.content == '{"intent":"total","params":{}}'


def test_fallback_chat_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Если fallback-вызов ``chat`` тоже падает — наружу LLMError, без второго fallback."""
    client = _client()
    calls: list[dict[str, Any]] = []

    def fake_post(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(payload)
        raise LLMError("HTTP 400: tools are not supported")

    monkeypatch.setattr(client, "_post_json", fake_post)
    with pytest.raises(LLMError):
        client.complete([{"role": "user", "content": "?"}], tools=build_tools())

    # Один вызов с tools + один fallback-вызов без tools; второго fallback нет.
    assert len(calls) == 2


def test_multiple_tool_calls_are_all_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    """Несколько tool_calls попадают в ``CompletionResult`` целиком (граф отклонит)."""
    client = _client()
    body = _body(
        {
            "content": "",
            "tool_calls": [
                _tool_call("total", '{"start_date": "2024-01-01"}'),
                _tool_call("timeseries", '{"freq": "M"}'),
            ],
        }
    )
    monkeypatch.setattr(client, "_post_json", lambda payload: body)

    result = client.complete([{"role": "user", "content": "?"}], tools=build_tools())

    assert result.tool_calls == (
        ToolCall(name="total", arguments={"start_date": "2024-01-01"}),
        ToolCall(name="timeseries", arguments={"freq": "M"}),
    )


def test_broken_arguments_json_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Битый JSON в ``arguments`` → LLMError."""
    client = _client()
    body = _body(
        {"content": "", "tool_calls": [_tool_call("total", "{not valid json")]}
    )
    monkeypatch.setattr(client, "_post_json", lambda payload: body)

    with pytest.raises(LLMError):
        client.complete([{"role": "user", "content": "?"}], tools=build_tools())


def test_tool_calls_and_content_are_both_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    """tool_calls + непустой content сохраняются в обеих ветках результата."""
    client = _client()
    body = _body(
        {
            "content": "пояснение модели",
            "tool_calls": [_tool_call("total", '{"start_date": "2024-01-01"}')],
        }
    )
    monkeypatch.setattr(client, "_post_json", lambda payload: body)

    result = client.complete([{"role": "user", "content": "?"}], tools=build_tools())

    assert result.content == "пояснение модели"
    assert result.tool_calls == (ToolCall(name="total", arguments={"start_date": "2024-01-01"}),)


@pytest.mark.parametrize("body", [{"choices": []}, {"choices": [{}]}, {}])
def test_empty_choices_or_message_raises(
    monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]
) -> None:
    """Пустой ``choices``/``message`` → LLMError."""
    client = _client()
    monkeypatch.setattr(client, "_post_json", lambda payload: body)

    with pytest.raises(LLMError):
        client.complete([{"role": "user", "content": "?"}], tools=build_tools())


def test_no_tools_uses_text_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Без tools — прежний текстовый JSON-режим (регрессия не сломана)."""
    client = _client()
    sent: list[dict[str, Any]] = []

    def fake_post(payload: dict[str, Any]) -> dict[str, Any]:
        sent.append(payload)
        return _body({"content": "text answer"})

    monkeypatch.setattr(client, "_post_json", fake_post)
    result = client.complete([{"role": "user", "content": "?"}], tools=None)

    assert result.content == "text answer"
    assert result.tool_calls == ()
    assert "tools" not in sent[0]

def test_timeout_error_names_configured_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Таймаут называет заданный лимит времени, а не безликое «сеть недоступна»."""
    import urllib.request

    settings = LLMSettings(
        provider="ollama",
        model="qwen3:4b",
        api_key="ollama",
        base_url="http://localhost:11434/v1",
        timeout=1200,
    )
    client = LLMClient(settings)

    def urlopen(*args: object, **kwargs: object) -> object:
        raise TimeoutError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    with pytest.raises(LLMError, match="не ответил за 1200"):
        client.chat([{"role": "user", "content": "hi"}])
