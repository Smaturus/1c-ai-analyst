"""Инструменты агента: 7 бизнес-функций + clarify/refuse и строгий разбор вызова."""

from __future__ import annotations

import pytest

from onec_analyst.agent.tools import (
    BUSINESS_TOOL_NAMES,
    CLARIFY_KEYS,
    META_TOOL_NAMES,
    TOOL_NAMES,
    ParsedCall,
    ToolCallError,
    build_agent_tools,
    parse_arguments_dsl,
    parse_completion,
    schema_signature,
    tool_schema,
    validate_meta_arguments,
)
from onec_analyst.llm_agent import INTENT_NAMES, CompletionResult, ToolCall

from tests.agent_support import (
    clarify_response,
    empty_response,
    json_response,
    legacy_json_response,
    refuse_response,
    text_response,
    tool_response,
    two_tool_response,
)


def test_exactly_seven_business_tools_and_two_meta() -> None:
    """Бизнес-инструментов семь, плюс ровно два мета-инструмента."""
    tools = build_agent_tools()
    names = [tool["function"]["name"] for tool in tools]

    assert len(tools) == 9
    assert tuple(BUSINESS_TOOL_NAMES) == INTENT_NAMES
    assert tuple(names[:7]) == INTENT_NAMES
    assert tuple(names[7:]) == META_TOOL_NAMES
    assert set(TOOL_NAMES) == set(names)


def test_business_schemas_are_generated_from_pydantic() -> None:
    """Схемы — из Pydantic-моделей: period2 обязателен у интентов сравнения."""
    for intent in ("compare_top_products", "counterparty_dynamics", "group_dynamics"):
        schema = tool_schema(intent)
        assert "period2" in schema["required"], intent
        assert "period1" in schema["required"], intent
    total = tool_schema("total")
    assert set(total.get("required") or ()) == set()


def test_clarify_schema_uses_canonical_keys() -> None:
    """``clarify.missing`` ограничен каноническими ключами."""
    schema = tool_schema("clarify")
    missing = schema["properties"]["missing"]

    assert missing["items"]["enum"] == list(CLARIFY_KEYS)
    assert schema["required"] == ["missing", "question"]
    assert tool_schema("refuse")["required"] == ["reason"]


def test_tool_signature_marks_optional_parameters() -> None:
    """Подпись для промпта строится из схемы и помечает необязательные поля."""
    signature = next(
        schema_signature(tool)
        for tool in build_agent_tools()
        if tool["function"]["name"] == "compare_top_products"
    )

    assert signature.startswith("compare_top_products(")
    assert "period1:" in signature and "period2:" in signature
    assert "top_n?" in signature


# --------------------------------------------------------------------------- #
# Ровно один вызов
# --------------------------------------------------------------------------- #
def test_single_tool_call_is_parsed() -> None:
    """Один нативный tool call нормализуется в имя + аргументы."""
    call = parse_completion(tool_response("total", {"start_date": "2024-01-01"}))

    assert call == ParsedCall(
        name="total",
        arguments={"start_date": "2024-01-01"},
        raw=call.raw,
    )
    assert call.raw


def test_json_adapter_is_equal_path() -> None:
    """JSON-адаптер даёт тот же результат, что нативный tool call."""
    parsed = parse_completion(json_response("timeseries", {"freq": "M"}))
    native = parse_completion(tool_response("timeseries", {"freq": "M"}))

    assert parsed.name == native.name == "timeseries"
    assert parsed.arguments == native.arguments == {"freq": "M"}


def test_legacy_intent_params_is_accepted() -> None:
    """Старый формат ``{"intent": ..., "params": ...}`` — тот же контракт DSL."""
    call = parse_completion(legacy_json_response("total", {"start_date": "2024-01-01"}))

    assert call.name == "total"
    assert call.arguments == {"start_date": "2024-01-01"}


@pytest.mark.parametrize(
    ("result", "marker"),
    [
        (empty_response(), "инструмент"),
        (text_response("Я думаю, это про продажи"), "инструмент"),
        (two_tool_response(), "инструментов"),
        (json_response("unknown_tool", {}), "Неизвестный инструмент"),
        (CompletionResult(content='{"tool": "total", "arguments": "нет"}'), "Аргументы"),
    ],
)
def test_bad_tool_call_is_an_error(result: CompletionResult, marker: str) -> None:
    """0/2 вызова, свободный текст, неизвестное имя и кривые аргументы — ошибка."""
    with pytest.raises(ToolCallError) as excinfo:
        parse_completion(result)

    assert marker.lower() in str(excinfo.value).lower()


def test_two_native_tool_calls_are_rejected() -> None:
    """Два tool call от OpenAI-совместимого провайдера не сворачиваются в один."""
    result = CompletionResult(
        tool_calls=(ToolCall("total", {}), ToolCall("total", {}))
    )

    with pytest.raises(ToolCallError):
        parse_completion(result)


# --------------------------------------------------------------------------- #
# Мета-инструменты
# --------------------------------------------------------------------------- #
def test_clarify_arguments_are_validated() -> None:
    """``clarify`` без missing и с неканоническим ключом отклоняется."""
    validate_meta_arguments(parse_completion(clarify_response(["period2"])))

    with pytest.raises(ToolCallError):
        validate_meta_arguments(
            ParsedCall(name="clarify", arguments={"missing": [], "question": "Какой?"})
        )
    with pytest.raises(ToolCallError):
        validate_meta_arguments(
            ParsedCall(
                name="clarify",
                arguments={"missing": ["период"], "question": "Какой?"},
            )
        )


def test_refuse_requires_reason() -> None:
    """Отказ без причины невозможен."""
    validate_meta_arguments(parse_completion(refuse_response("Не поддерживается")))

    with pytest.raises(ToolCallError):
        validate_meta_arguments(ParsedCall(name="refuse", arguments={"reason": " "}))


def test_fix_arguments_accept_only_json_object() -> None:
    """``fix_dsl`` возвращает JSON аргументов, а не текст."""
    assert parse_arguments_dsl('{"start_date": "2024-01-01"}', intent="total") == {
        "start_date": "2024-01-01"
    }
    assert parse_arguments_dsl(
        '{"tool": "total", "arguments": {"metric": "revenue"}}', intent="total"
    ) == {"metric": "revenue"}
    assert parse_arguments_dsl('{"params": {"freq": "M"}}', intent="timeseries") == {
        "freq": "M"
    }

    with pytest.raises(ToolCallError):
        parse_arguments_dsl("исправил", intent="total")
