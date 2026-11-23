"""Подсказки уточнения следуют за годами из вопроса, а не за статичным 2023/2024."""

from __future__ import annotations

from datetime import date

from onec_analyst.llm_agent import (
    INTENT_EXAMPLES,
    INTENT_NAMES,
    RuleBasedPlanner,
    _year_aware_suggestions,
)

TODAY = date(2026, 11, 22)
JSON_DSL_2022 = (
    '{"intent": "total", "params": '
    '{"start_date": "2022-01-01", "end_date": "2022-12-31"}}'
)


def test_suggestions_use_year_from_question() -> None:
    """Если в вопросе есть 2022 — подсказки про 2022, без 2023/2024."""
    suggestions = _year_aware_suggestions("Покажи итог за 2022 год")

    assert suggestions
    assert all("2022" in item for item in suggestions)
    assert not any("2023" in item or "2024" in item for item in suggestions)


def test_suggestions_for_two_years_start_with_comparison() -> None:
    """Два года в вопросе — первая подсказка про сравнение именно этих лет."""
    suggestions = _year_aware_suggestions("сравни 2021 и 2022")

    assert suggestions[0] == "Сравни топ-5 товаров за 2021 и 2022 годы"


def test_no_years_gives_empty_list() -> None:
    """Без годов функция не выдумывает периоды — вызывающий код берёт шаблоны."""
    assert _year_aware_suggestions("покажи что-нибудь интересное") == []


def test_planner_clarify_follows_years() -> None:
    """Интент не разобран, но годы есть — подсказки просят 2022, а не 2023/2024."""
    decision = RuleBasedPlanner().plan(JSON_DSL_2022, today=TODAY)

    assert decision.intent == "clarification"
    assert decision.clarification is not None
    suggestions = decision.clarification.suggestions
    assert suggestions
    assert any("2022" in item for item in suggestions)
    assert not any("2023" in item or "2024" in item for item in suggestions)
    assert decision.dsl.get("suggestions") == suggestions


def test_planner_clarify_without_years_uses_examples() -> None:
    """Без годов в вопросе остаётся прежний набор примеров по интентам."""
    decision = RuleBasedPlanner().plan("Покажи что-нибудь интересное", today=TODAY)

    assert decision.clarification is not None
    assert list(decision.clarification.suggestions) == [
        INTENT_EXAMPLES[name] for name in INTENT_NAMES
    ]


def test_dynamics_missing_period_follows_years() -> None:
    """Уточнение «за какие два периода» тоже подставляет год из вопроса."""
    decision = RuleBasedPlanner().plan(
        "У каких контрагентов упали продажи за 2022 год", today=TODAY
    )

    assert decision.intent == "clarification"
    assert decision.clarification is not None
    assert decision.clarification.missing == ["period1", "period2"]
    assert any("2022" in item for item in decision.clarification.suggestions)
