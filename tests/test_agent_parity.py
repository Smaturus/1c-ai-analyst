"""Паритет агентного и офлайн-путей на простых вопросах (intent + params)."""

from __future__ import annotations

from datetime import date

import pytest

from onec_analyst.agent.graph import run_agent_graph
from onec_analyst.agent.state import initial_state
from onec_analyst.llm_agent import (
    AgentDecision,
    ClarificationPlan,
    RuleBasedPlanner,
)

from tests.agent_support import (
    FakeLLMClient,
    clarify_response,
    iso,
    json_response,
    refuse_response,
)

TODAY = date(2026, 11, 18)

QUESTIONS = (
    "Итог за 2024",
    "Топ-3 товара по выручке за 2024",
    "Какие 5 товаров продаются хуже остальных за 2024 год?",
    "Как менялись продажи по месяцам в 2023 году?",
    "Покажи товары с отрицательной выручкой за 2024 год",
)


def _graph_decision(question: str) -> AgentDecision:
    """Прогнать вопрос через граф с моделью, повторяющей решение правил."""
    expected = RuleBasedPlanner().plan(question, today=TODAY)
    if expected.status == "refuse":
        response = refuse_response(
            expected.refusal_reason or "Не поддерживается"
        )
    elif expected.intent == "clarification":
        plan = expected.clarification
        missing = list(plan.missing) if plan is not None else []
        response = clarify_response(missing or ["intent"], plan.question if plan else "Уточните запрос")
    else:
        response = json_response(str(expected.intent), iso(dict(expected.params or {})))
    client = FakeLLMClient([response])
    final = run_agent_graph(
        initial_state(question, today=TODAY, client=client), client=client
    )
    return final["decision"]


@pytest.mark.parametrize("question", QUESTIONS)
def test_simple_questions_match_rules(question: str) -> None:
    """После нормализации граф даёт тот же intent и params, что и правила."""
    expected = RuleBasedPlanner().plan(question, today=TODAY)
    actual = _graph_decision(question)

    assert actual.status == "ok"
    assert actual.intent == expected.intent
    assert dict(actual.params) == dict(expected.params)
    assert actual.dsl["intent"] == expected.intent


def test_compare_intent_parity() -> None:
    """Сравнение топов: periods и top_n совпадают с правилами."""
    question = "Сравни топ-5 товаров за 2023 и 2024 годы"
    expected = RuleBasedPlanner().plan(question, today=TODAY)
    actual = _graph_decision(question)

    assert expected.intent == actual.intent == "compare_top_products"
    assert actual.params["top_n"] == expected.params["top_n"]
    assert iso(actual.params["period1"]) == iso(expected.params["period1"])
    assert iso(actual.params["period2"]) == iso(expected.params["period2"])


def test_clarification_parity() -> None:
    """Неоднозначный вопрос уточняется в обоих путях, без выдуманного интента."""
    question = "Сравни продажи"
    expected = RuleBasedPlanner().plan(question, today=TODAY)
    actual = _graph_decision(question)

    assert expected.intent == "clarification"
    assert actual.intent == "clarification"
    assert actual.status == "clarify"
    assert isinstance(actual.clarification, ClarificationPlan)


def test_graph_does_not_change_contract_params() -> None:
    """Граф не добавляет полей сверх канонических (extra=forbid)."""
    actual = _graph_decision("Топ-3 товара по выручке за 2024")

    allowed = {
        "metric",
        "limit",
        "ascending",
        "start_date",
        "end_date",
        "product_filter",
        "group_filter",
    }

    assert set(actual.params).issubset(allowed)


def test_default_limit_matches_rules_and_graph() -> None:
    """Без явного числа в вопросе limit=5 и в правилах, и в графе."""
    question = "Какие товары продаются хуже остальных за 2024 год?"
    expected = RuleBasedPlanner().plan(question, today=TODAY)
    actual = _graph_decision(question)

    assert expected.params["limit"] == 5
    assert actual.status == "ok"
    assert actual.params["limit"] == expected.params["limit"] == 5


def test_average_check_refuse_parity() -> None:
    """Средний чек: refuse и в правилах, и в графе."""
    question = "Какой средний чек за 2024 год?"
    expected = RuleBasedPlanner().plan(question, today=TODAY)
    actual = _graph_decision(question)

    assert expected.status == "refuse"
    assert actual.status == "refuse"
    assert actual.refusal_reason == expected.refusal_reason
