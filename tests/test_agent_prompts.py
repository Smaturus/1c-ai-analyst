"""Промпт агента: правила слабых зон, few-shot от today, схемы не дублируются.

Проверяется, что системный промпт ``agent_decision`` содержит правила для
известных слабых зон (ascending, clarify, хронология периодов, product_filter,
относительные даты), что few-shot примеры считают даты от ``today``, и что
каталог инструментов собирается из Pydantic-схем, а не пишется руками.
"""

from __future__ import annotations

from datetime import date

from onec_analyst.agent.prompts import (
    build_agent_messages,
    build_agent_system_prompt,
    schema_signature,
    tool_catalog,
)
from onec_analyst.agent.tools import build_agent_tools

TODAY = date(2024, 5, 15)


def _prompt(today: date = TODAY, **kwargs) -> str:
    return build_agent_system_prompt(
        today=today,
        previous_dsl=kwargs.get("previous_dsl"),
        result_entities=kwargs.get("result_entities"),
    )


def test_rules_cover_weak_zones() -> None:
    """Слабые зоны покрыты явными правилами (ascending, clarify, периоды, фильтры)."""
    prompt = _prompt()

    assert "ascending" in prompt and "антитоп" in prompt
    assert "УТОЧНЯЙ, НЕ УГАДЫВАЙ" in prompt
    assert "период уже указан" in prompt
    assert "Покажи продажи 2025" in prompt
    assert "period1 — более ранний период" in prompt
    assert "по сравнению" in prompt
    assert "ОДНА строка" in prompt  # product_filter не список
    assert "Относительные даты считай строго" in prompt


def test_few_shot_uses_today_for_relative_dates() -> None:
    """Few-shot «в этом месяце»/«прошлый год» считаются от переданной даты."""
    prompt = _prompt(TODAY)

    # «в этом месяце» → май 2024.
    assert "2024-05-01" in prompt and "2024-05-31" in prompt
    # «прошлый год» → 2023, «позапрошлый» → 2022.
    assert "итог за 2023 год" in prompt
    assert "period1 = 2022" in prompt and "period2 = 2023" in prompt


def test_few_shot_examples_cover_counterparty_and_filter() -> None:
    """Примеры показывают период сравнения и product_filter одной строкой."""
    prompt = _prompt()

    assert "period1 = 1-й квартал (2024-01-01" in prompt
    assert "product_filter = «Кофе» (одна строка" in prompt
    assert "ascending=true" in prompt
    assert "уточняющий вопрос" in prompt


def test_tool_catalog_is_generated_from_schemas() -> None:
    """Каталог инструментов собран из Pydantic-схем, а не списка-дубликата."""
    tools = build_agent_tools()
    catalog = tool_catalog(tools)

    for tool in tools:
        assert schema_signature(tool) in catalog


def test_system_prompt_uses_schema_signatures_not_handwritten() -> None:
    """Подписи инструментов в промпте совпадают с подписями из схем.

    Если поле Pydantic-модели изменится, подпись (и промпт) изменятся сами —
    список параметров не поддерживается вручную (ADR 0013 D1).
    """
    prompt = _prompt()

    for tool in build_agent_tools():
        assert schema_signature(tool) in prompt


def test_messages_carry_question_and_today() -> None:
    """Сообщения агента: система с today + вопрос пользователя."""
    messages = build_agent_messages("Сколько продали?", today=TODAY)

    assert messages[0]["role"] == "system"
    assert "2024-05-15" in messages[0]["content"]
    assert messages[1] == {"role": "user", "content": "Сколько продали?"}
