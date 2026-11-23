"""Проигрыватель кассет GoldSet: детерминированно воспроизводит ответы модели.

``CassetteClient`` реализует тот же интерфейс, что и LLM-клиенты графа
(``complete(messages, tools) -> CompletionResult``), но не ходит в сеть:
ответы отдаются по очереди из кассеты. Используется replay-тестом в CI, чтобы
прогнать парсер → Pydantic → ``fix_dsl`` → маршрутизацию → факт-чек без ключей
и без сети.

Здесь же лежат функции конвертации ``CompletionResult <->`` записи кассеты,
которые использует и ``tools/record_cassettes.py``: единственный источник
формата, чтобы запись и replay не разошлись.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from onec_analyst.llm_agent import CompletionResult, LLMError, ToolCall

#: Признак синтетического примера формата (в eval не считается).
SYNTHETIC_SOURCE = "synthetic_example"


def completion_to_entry(result: CompletionResult) -> dict[str, Any]:
    """Один ``CompletionResult`` → одна запись кассеты.

    Приоритет tool call: если модель вернула вызов инструмента, текст (обычно
    пустой или преамбула) не хранится — граф разбирает вызов первым. JSON-ответ
    провайдера без tool calling хранится как ``{"content": "..."}``.
    """
    if result.tool_calls:
        call = result.tool_calls[0]
        return {"tool_call": {"name": call.name, "arguments": dict(call.arguments)}}
    return {"content": result.content or ""}


def entry_to_completion(entry: Mapping[str, Any]) -> CompletionResult:
    """Запись кассеты → ``CompletionResult`` (обратная ``completion_to_entry``)."""
    if "tool_call" in entry:
        call = entry["tool_call"]
        name = str(call.get("name") or "")
        arguments = call.get("arguments") or {}
        if not name:
            raise LLMError("Кассета: tool_call без имени инструмента")
        if not isinstance(arguments, dict):
            raise LLMError("Кассета: аргументы tool_call должны быть объектом")
        return CompletionResult(tool_calls=(ToolCall(name=name, arguments=dict(arguments)),))
    if "content" in entry:
        content = entry["content"]
        if not isinstance(content, str):
            raise LLMError("Кассета: content должен быть строкой")
        return CompletionResult(content=content)
    raise LLMError("Кассета: запись не содержит ни tool_call, ни content")


class CassetteClient:
    """LLM-клиент, отдающий заранее записанные ответы модели без сети.

    Parameters
    ----------
    cassette:
        Словарь кассеты (``json.loads`` файла). Поле ``responses`` — список
        записей; старый одиночный ``response`` поддерживается как список из
        одного элемента.
    """

    def __init__(self, cassette: Mapping[str, Any]) -> None:
        raw = cassette.get("responses")
        if isinstance(raw, list):
            responses = list(raw)
        elif isinstance(cassette.get("response"), dict):
            responses = [cassette["response"]]
        else:
            raise LLMError("Кассета не содержит поле responses (или response)")
        if not responses:
            raise LLMError("Кассета содержит пустой список responses")
        self.case_id = str(cassette.get("case_id") or "")
        self.source = str(cassette.get("source") or "")
        self.provider = str(cassette.get("provider") or "")
        self.model = str(cassette.get("model") or "")
        self.recorded_at = str(cassette.get("recorded_at") or "")
        self.recorded_outcome = dict(cassette.get("recorded_outcome") or {})
        self._responses = responses
        self._cursor = 0

    @classmethod
    def load(cls, path: str | Path) -> "CassetteClient":
        """Прочитать кассету из JSON-файла."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise LLMError(f"Кассета {path} должна быть JSON-объектом")
        return cls(data)

    @property
    def supports_tools(self) -> bool:
        """Нативный tool calling, если хоть одна запись — ``tool_call``."""
        return any("tool_call" in entry for entry in self._responses)

    def complete(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
    ) -> CompletionResult:
        """Вернуть следующий записанный ответ; при исчерпании — :class:`LLMError`.

        Аргументы ``messages``/``tools`` игнорируются: replay воспроизводит
        записанный ответ, а не вызывает модель.
        """
        del messages, tools  # детерминированный replay: входы не используются
        if self._cursor >= len(self._responses):
            raise LLMError(
                f"Кассета {self.case_id!r}: ответы исчерпаны "
                f"({self._cursor} из {len(self._responses)})"
            )
        entry = self._responses[self._cursor]
        self._cursor += 1
        return entry_to_completion(entry)

    def exhausted(self) -> bool:
        """Все ли записанные ответы были отданы."""
        return self._cursor >= len(self._responses)

    def remaining(self) -> int:
        """Сколько записанных ответов ещё не отдано."""
        return len(self._responses) - self._cursor


def iter_cassette_paths(cassette_dir: str | Path) -> list[Path]:
    """Все JSON-кассеты каталога в порядке имени (для параметризации)."""
    directory = Path(cassette_dir)
    return sorted(directory.glob("*.json"))


__all__ = [
    "SYNTHETIC_SOURCE",
    "CassetteClient",
    "completion_to_entry",
    "entry_to_completion",
    "iter_cassette_paths",
]
