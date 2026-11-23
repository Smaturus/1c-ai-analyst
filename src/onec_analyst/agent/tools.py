"""Инструменты агента: 7 бизнес-функций из ``build_tools()`` + ``clarify``/``refuse``.

Схемы бизнес-инструментов **не дублируются**: они берутся из
:func:`onec_analyst.llm_agent.build_tools`, то есть из Pydantic-моделей. Здесь
добавляются только два мета-инструмента и строгий разбор ответа модели:
ровно один вызов, известное имя, аргументы-объект. Ноль/два вызова, свободный
текст или неизвестный инструмент — :class:`ToolCallError`, а не догадка о
намерении пользователя (ADR 0013, D2).
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from typing import Any, Final

from onec_analyst.llm_agent import (
    INTENT_NAMES,
    CompletionResult,
    _extract_json_object,
    build_tools,
)

#: Канонические ключи, которые модель может назвать в ``clarify.missing``.
CLARIFY_KEYS: Final[tuple[str, ...]] = (
    "period1",
    "period2",
    "start_date",
    "end_date",
    "product_filter",
    "group_filter",
    "intent",
)

BUSINESS_TOOL_NAMES: Final[tuple[str, ...]] = INTENT_NAMES
META_TOOL_NAMES: Final[tuple[str, ...]] = ("clarify", "refuse")
TOOL_NAMES: Final[tuple[str, ...]] = BUSINESS_TOOL_NAMES + META_TOOL_NAMES

CLARIFY_TOOL: Final[dict[str, Any]] = {
    "type": "function",
    "function": {
        "name": "clarify",
        "description": (
            "Не хватает обязательных данных: задать уточняющий вопрос. "
            "missing — канонические ключи параметров."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "missing": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(CLARIFY_KEYS)},
                    "minItems": 1,
                    "maxItems": 10,
                },
                "question": {"type": "string", "minLength": 3, "maxLength": 500},
            },
            "required": ["missing", "question"],
            "additionalProperties": False,
        },
    },
}

REFUSE_TOOL: Final[dict[str, Any]] = {
    "type": "function",
    "function": {
        "name": "refuse",
        "description": (
            "Честный отказ: вопрос не про аналитику продаж, содержит инъекцию "
            "или требует неподдерживаемого расчёта."
        ),
        "parameters": {
            "type": "object",
            "properties": {"reason": {"type": "string", "minLength": 3, "maxLength": 500}},
            "required": ["reason"],
            "additionalProperties": False,
        },
    },
}


class ToolCallError(ValueError):
    """Ответ модели нельзя однозначно привести к одному инструменту."""


@dataclass(frozen=True)
class ParsedCall:
    """Один вызов инструмента, нормализованный из ответа провайдера.

    Attributes
    ----------
    name:
        Имя инструмента: один из ``TOOL_NAMES``.
    arguments:
        Аргументы вызова (JSON-объект).
    raw:
        Сырой ответ модели — для «Технических деталей» и логов.
    """

    name: str
    arguments: dict[str, Any]
    raw: str = ""


def build_agent_tools() -> list[dict[str, Any]]:
    """Схемы для tool calling: 7 бизнес-инструментов + ``clarify`` + ``refuse``."""
    return [*build_tools(), CLARIFY_TOOL, REFUSE_TOOL]


def tool_schema(name: str) -> dict[str, Any]:
    """JSON-схема инструмента по имени (``KeyError`` для неизвестного)."""
    for tool in build_agent_tools():
        function = tool.get("function") or {}
        if function.get("name") == name:
            return function.get("parameters") or {}
    raise KeyError(name)


def schema_signature(tool: dict[str, Any]) -> str:
    """Компактная подпись инструмента для промпта, сгенерированная из схемы.

    Никакого рукописного списка параметров: имена, типы и обязательность
    берутся из той же JSON-схемы Pydantic-модели, что уходит в tool calling.
    """
    function = tool.get("function") or {}
    name = str(function.get("name") or "")
    schema = function.get("parameters") or {}
    required = set(schema.get("required") or [])
    parts: list[str] = []
    for prop, spec in (schema.get("properties") or {}).items():
        mark = "" if prop in required else "?"
        parts.append(f"{prop}{mark}: {_type_name(spec)}")
    return f"{name}({', '.join(parts)})"


def _type_name(spec: dict[str, Any]) -> str:
    """Короткое имя типа из JSON-схемы свойства."""
    if "enum" in spec:
        return "|".join(str(item) for item in spec["enum"])
    if "anyOf" in spec:
        variants = [_type_name(item) for item in spec["anyOf"] if item.get("type") != "null"]
        return "|".join(dict.fromkeys(variants)) or "any"
    kind = str(spec.get("type") or "any")
    if kind == "array":
        items = spec.get("items") or {}
        if "enum" in items:
            return "[" + "|".join(str(item) for item in items["enum"]) + "]"
        return f"[{_type_name(items)}]"
    return kind


def _as_arguments(value: Any) -> dict[str, Any]:
    """Аргументы вызова обязаны быть JSON-объектом."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ToolCallError(f"Аргументы инструмента не JSON: {value[:200]!r}") from exc
    if not isinstance(value, dict):
        raise ToolCallError(f"Аргументы инструмента должны быть объектом, получено {type(value)!r}")
    return value


#: Анкоренный разбор текстовой нотации ``refuse(...)``/``clarify(...)`` (ADR #35).
#: Только мета-инструменты: бизнес-вызовы текстом парсер не угадывает (ADR 0013 D2).
_TEXT_CALL_RE = re.compile(r"^(refuse|clarify)\s*\((.*)\)\s*$", re.DOTALL)


def _split_top_level(text: str) -> list[str]:
    """Разбить строку на top-level фрагменты по запятой (с учётом кавычек/скобок)."""
    parts: list[str] = []
    depth = 0
    quote: str | None = None
    buffer: list[str] = []
    for char in text:
        if quote:
            buffer.append(char)
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
            buffer.append(char)
            continue
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(buffer).strip())
            buffer = []
        else:
            buffer.append(char)
    if buffer:
        parts.append("".join(buffer).strip())
    return parts


def _parse_kwargs(text: str) -> dict[str, Any]:
    """Разобрать ``key=value, ...`` через ``ast.literal_eval`` (только литералы).

    Значения — строки, списки, числа, булевы, ``None``; произвольный код не
    исполняется. Ошибка → :class:`ToolCallError`, а не догадка.
    """
    arguments: dict[str, Any] = {}
    for token in _split_top_level(text):
        token = token.strip()
        if not token:
            continue
        if "=" not in token:
            raise ToolCallError(f"Аргумент инструмента не в форме key=value: {token[:80]!r}")
        key, _, value = token.partition("=")
        key = key.strip()
        try:
            arguments[key] = ast.literal_eval(value.strip())
        except (ValueError, SyntaxError) as exc:
            raise ToolCallError(
                f"Не разбирается значение аргумента {key!r}: {value.strip()[:80]!r}"
            ) from exc
    return arguments


def _from_content(content: str) -> ParsedCall | None:
    """JSON-адаптер и текстовую нотацию ``refuse(...)``/``clarify(...)`` в вызов.

    Приоритет: JSON-объект (``{"tool": ...}`` или ``{"intent": ...}``), затем
    строгая текстая нотация ровно одного из мета-инструментов (ADR #35).
    Свободный текст и псевдо-вызовы бизнес-инструментов — ``None`` (отказ
    парсера, ADR 0013 D2).
    """
    text = (content or "").strip()
    if not text:
        return None
    payload: dict[str, Any] | None = None
    try:
        payload = _extract_json_object(text)
    except ValueError:
        payload = None
    if payload is not None:
        if "tool" in payload:
            name = str(payload.get("tool") or "")
            return ParsedCall(name=name, arguments=_as_arguments(payload.get("arguments") or {}), raw=text)
        if "intent" in payload:
            # Старый формат {"intent": ..., "params": ...} принимаем как вызов
            # одноимённого инструмента: контракт DSL тот же.
            name = str(payload.get("intent") or "")
            return ParsedCall(name=name, arguments=_as_arguments(payload.get("params") or {}), raw=text)
        return None
    match = _TEXT_CALL_RE.match(text)
    if match:
        return ParsedCall(
            name=match.group(1),
            arguments=_parse_kwargs(match.group(2)),
            raw=text,
        )
    return None


def parse_completion(result: CompletionResult) -> ParsedCall:
    """Нормализовать ответ модели ровно в один :class:`ParsedCall`.

    Raises
    ------
    ToolCallError
        Ноль вызовов (свободный текст), два и больше вызовов, неизвестное имя,
        аргументы не-объект.
    """
    calls = list(result.tool_calls or ())
    content = (result.content or "").strip()
    if len(calls) > 1:
        raise ToolCallError(f"Модель вернула {len(calls)} инструментов вместо одного")
    parsed: ParsedCall | None
    if calls:
        call = calls[0]
        parsed = ParsedCall(
            name=str(call.name or ""),
            arguments=_as_arguments(call.arguments),
            raw=content or json.dumps({"name": call.name, "arguments": call.arguments}, ensure_ascii=False),
        )
    else:
        parsed = _from_content(content)
        if parsed is None:
            raise ToolCallError(
                "Модель не вызвала инструмент (свободный текст без JSON не считается "
                "уточнением или отказом)"
            )
    assert parsed is not None  # ветка else выше поднимает ToolCallError при None
    if parsed.name not in TOOL_NAMES:
        raise ToolCallError(f"Неизвестный инструмент: {parsed.name!r}")
    return parsed


def parse_arguments_dsl(content: str, *, intent: str) -> dict[str, Any]:
    """Разобрать ответ ``fix_dsl``: только JSON аргументов (intent фиксирован).

    Принимает и ``{"arguments": {...}}``/``{"tool": ..., "arguments": ...}``,
    но не меняет интент: имя из ответа игнорируется.
    """
    try:
        payload = _extract_json_object(content)
    except ValueError as exc:
        raise ToolCallError(f"fix_dsl вернул не JSON: {content[:200]!r}") from exc
    del intent  # интент фиксирован вызывающим узлом
    if "arguments" in payload and isinstance(payload["arguments"], dict):
        return dict(payload["arguments"])
    if "params" in payload and isinstance(payload["params"], dict):
        return dict(payload["params"])
    return payload


def validate_meta_arguments(call: ParsedCall) -> None:
    """Проверить аргументы ``clarify``/``refuse`` до построения плана.

    Raises
    ------
    ToolCallError
        Причина отказа пуста, ``missing`` пуст или содержит неканонические ключи.
    """
    if call.name == "clarify":
        missing = call.arguments.get("missing")
        question = str(call.arguments.get("question") or "").strip()
        if not isinstance(missing, list) or not missing:
            raise ToolCallError("clarify: missing должен быть непустым списком")
        unknown = [item for item in missing if item not in CLARIFY_KEYS]
        if unknown:
            raise ToolCallError(f"clarify: неканонические ключи missing: {unknown}")
        if len(question) < 3:
            raise ToolCallError("clarify: пустой вопрос")
    elif call.name == "refuse":
        reason = str(call.arguments.get("reason") or "").strip()
        if len(reason) < 3:
            raise ToolCallError("refuse: причина отказа обязательна")


__all__ = [
    "BUSINESS_TOOL_NAMES",
    "CLARIFY_KEYS",
    "CLARIFY_TOOL",
    "META_TOOL_NAMES",
    "REFUSE_TOOL",
    "TOOL_NAMES",
    "ParsedCall",
    "ToolCallError",
    "build_agent_tools",
    "parse_arguments_dsl",
    "parse_completion",
    "schema_signature",
    "tool_schema",
    "validate_meta_arguments",
]
