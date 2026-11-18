"""Общие fake-объекты для тестов агента: клиент без сети и ответы-заготовки.

Модуль не собирается pytest'ом (имя не ``test_*``): это библиотека тестов.
Клиент умеет возвращать последовательность ответов — так проверяется сценарий
«невалидно → fix_dsl → валидно» без реального провайдера.
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any, Callable, Sequence

from onec_analyst.llm_agent import CompletionResult, ToolCall

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


class FakeLLMClient:
    """LLM-клиент без сети: отдаёт заранее заданные ответы по очереди.

    Parameters
    ----------
    responses:
        Последовательность :class:`CompletionResult` или функций
        ``messages -> CompletionResult``. Когда очередь исчерпана, повторяется
        последний ответ (удобно для «модель всегда отвечает так»).
    supports_tools:
        Имитировать нативный tool calling (GigaChat) или JSON-адаптер.

    Attributes
    ----------
    calls:
        Сколько раз вызывали ``complete``.
    requests:
        Сообщения и tools каждого вызова (для проверки промпта и памяти).
    """

    def __init__(
        self,
        responses: Sequence[CompletionResult | Callable[[list[dict[str, str]]], CompletionResult]],
        *,
        supports_tools: bool = False,
    ) -> None:
        if not responses:
            raise ValueError("Нужен хотя бы один ответ модели")
        self.responses = list(responses)
        self.supports_tools = supports_tools
        self.calls = 0
        self.requests: list[dict[str, Any]] = []

    def complete(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
    ) -> CompletionResult:
        """Вернуть очередной ответ и запомнить запрос."""
        self.requests.append({"messages": messages, "tools": tools})
        index = min(self.calls, len(self.responses) - 1)
        response = self.responses[index]
        self.calls += 1
        if callable(response):
            return response(messages)
        return response

    def chat(self, messages: list[dict[str, str]]) -> str:
        """Текстовый вызов (для классификатора уточнений)."""
        return self.complete(messages, tools=None).content


def tool_response(name: str, arguments: dict[str, Any]) -> CompletionResult:
    """Ответ модели как нативный tool call (GigaChat)."""
    return CompletionResult(tool_calls=(ToolCall(name=name, arguments=arguments),))


def json_response(name: str, arguments: dict[str, Any]) -> CompletionResult:
    """Ответ JSON-адаптера: ``{"tool": ..., "arguments": ...}``."""
    return CompletionResult(
        content=json.dumps({"tool": name, "arguments": arguments}, ensure_ascii=False)
    )


def legacy_json_response(intent: str, params: dict[str, Any]) -> CompletionResult:
    """Старый формат ``{"intent": ..., "params": ...}`` (тоже принимается)."""
    return CompletionResult(
        content=json.dumps({"intent": intent, "params": params}, ensure_ascii=False)
    )


def clarify_response(missing: Sequence[str], question: str = "Уточните запрос") -> CompletionResult:
    """Ответ-уточнение через инструмент ``clarify``."""
    return json_response("clarify", {"missing": list(missing), "question": question})


def refuse_response(reason: str) -> CompletionResult:
    """Ответ-отказ через инструмент ``refuse``."""
    return json_response("refuse", {"reason": reason})


def text_response(text: str) -> CompletionResult:
    """Свободный текст без вызова инструмента (должен стать handle_error)."""
    return CompletionResult(content=text)


def empty_response() -> CompletionResult:
    """Пустой ответ модели."""
    return CompletionResult()


def two_tool_response() -> CompletionResult:
    """Два tool call за один ответ (нарушение контракта)."""
    return CompletionResult(
        tool_calls=(
            ToolCall("total", {"start_date": "2024-01-01", "end_date": "2024-12-31"}),
            ToolCall("timeseries", {"freq": "M"}),
        )
    )


def message_text(messages: Sequence[dict[str, str]]) -> str:
    """Склеенный текст сообщений (для проверки, что контекст попал в промпт)."""
    return "\n".join(str(item.get("content") or "") for item in messages)


def context_dates(messages: Sequence[dict[str, str]]) -> list[str]:
    """ISO-даты, найденные в сообщениях (по ним fake «понимает» контекст)."""
    return _DATE_RE.findall(message_text(messages))


def echo_bounds_response(intent: str, extra: dict[str, Any] | None = None) -> Callable:
    """Ответ, который подставляет в аргументы даты, найденные в промпте.

    Имитирует послушную модель: если граф передал унаследованный период в
    тексте, модель использует его. Так проверяется, что наследование реально
    доходит до аргументов, а не остаётся в состоянии.
    """

    def _response(messages: list[dict[str, str]]) -> CompletionResult:
        dates = context_dates(messages)
        arguments: dict[str, Any] = dict(extra or {})
        if len(dates) >= 2:
            # Последние две даты — из вопроса с контекстом, а не «сегодня».
            arguments["start_date"] = dates[-2]
            arguments["end_date"] = dates[-1]
        return json_response(intent, arguments)

    return _response


def iso(value: Any) -> Any:
    """Привести даты/списки дат к JSON-совместимому виду для сравнения."""
    if isinstance(value, (date,)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [iso(item) for item in value]
    if isinstance(value, dict):
        return {key: iso(item) for key, item in value.items()}
    return value
