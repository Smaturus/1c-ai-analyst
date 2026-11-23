"""Защита метрики: «по количеству» — только когда пользователь просит штуки.

Дефект из приёмки: на вопрос «Топ-10 самых продаваемых товаров за прошлый год»
GigaChat вернул ``metric="quantity"``, и ответ строился по количеству
(«Кабель Iota … — 420 шт.») вместо выручки. Правила же всегда давали выручку —
значит, расходились два пути, и подмена модели ничем не ограничивалась.

Здесь проверяются три уровня:

* :func:`onec_analyst.llm_agent.guard_metric` — само правило;
* офлайн-правила на том же вопросе (регрессия);
* диспетчер — что подмена модели исправляется и в обычном, и в агентном пути,
  а явная просьба о штуках сохраняется.

Сеть не используется: планировщик и граф подменяются заглушками.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from onec_analyst.agent.prompts import build_agent_system_prompt
from onec_analyst.dispatcher import process_request
from onec_analyst.llm_agent import (
    SYSTEM_PROMPT,
    AgentDecision,
    LLMPlanner,
    LLMSettings,
    RuleBasedPlanner,
    guard_metric,
    is_quantity_question,
)
from onec_analyst.session import ConversationState

PROFILE = {"date_min": "2023-01-15", "date_max": "2024-03-05"}
#: Вопрос из дефекта: слово «продаваемых» про деньги, «10» — это limit.
SALES_QUESTION = "Топ-10 самых продаваемых товаров за прошлый год"
TODAY = date(2026, 11, 21)


def _configured_settings() -> LLMSettings:
    """Настройки «LLM настроена»: клиент создаётся, сетевого вызова нет."""
    return LLMSettings(provider="deepseek", model="deepseek-chat", api_key="test-key")


def _decision(**params: object) -> AgentDecision:
    """Решение top_bottom_products с заданными параметрами (source=llm)."""
    payload = {"intent": "top_bottom_products", "params": dict(params)}
    return AgentDecision(
        ok=True,
        intent="top_bottom_products",
        params=dict(params),
        dsl=payload,
        source="llm",
    )


class _FakePlanner:
    """Планировщик-заглушка: возвращает заранее заданное решение."""

    def __init__(self, decision: AgentDecision) -> None:
        self.decision = decision

    def plan(self, question, history=None, data_profile=None):
        return self.decision


# --------------------------------------------------------------------------- #
# Само правило
# --------------------------------------------------------------------------- #
def test_quantity_question_requires_explicit_word() -> None:
    """«Самых продаваемых» и «топ-10» — не про штуки."""
    assert not is_quantity_question(SALES_QUESTION)
    assert not is_quantity_question("Топ-5 лидеров по выручке за 2024")
    assert is_quantity_question("Топ-10 товаров по количеству за 2024")
    assert is_quantity_question("Сколько штук продали за 2024?")
    assert is_quantity_question("Топ-10 в единицах за 2024")


def test_guard_replaces_quantity_with_revenue() -> None:
    """Метрика меняется только она: limit, ascending и период остаются."""
    decision = _decision(
        metric="quantity",
        limit=10,
        ascending=False,
        start_date="2025-01-01",
        end_date="2025-12-31",
    )

    fixed = guard_metric(decision, SALES_QUESTION)

    assert fixed.params["metric"] == "revenue"
    assert fixed.params["limit"] == 10
    assert fixed.params["ascending"] is False
    assert fixed.params["start_date"] == "2025-01-01"
    assert fixed.params["end_date"] == "2025-12-31"
    # DSL в «Технических деталях» обязан совпадать с расчётом.
    assert fixed.dsl["params"]["metric"] == "revenue"
    # Исходное решение не мутируется: dataclass заморожен, словари скопированы.
    assert decision.params["metric"] == "quantity"
    assert decision.dsl["params"]["metric"] == "quantity"


def test_guard_keeps_explicit_quantity() -> None:
    """Явная просьба о штуках не переписывается."""
    decision = _decision(metric="quantity", limit=5)

    assert guard_metric(decision, "Топ-5 товаров по количеству за 2024") is decision


def test_guard_ignores_decisions_without_metric() -> None:
    """Итог и другие интенты без metric не трогаются."""
    decision = AgentDecision(
        ok=True,
        intent="total",
        params={"start_date": "2024-01-01", "end_date": "2024-12-31"},
        dsl={"intent": "total"},
        source="llm",
    )

    assert guard_metric(decision, SALES_QUESTION) is decision


def test_guard_ignores_errors_and_clarifications() -> None:
    """Отказ/ошибка не получают выдуманного DSL."""
    failed = AgentDecision(ok=False, error="Модель вернула невалидный JSON", source="llm")
    clarify = AgentDecision(
        ok=True,
        intent="clarification",
        params={"metric": "quantity"},
        source="llm",
        status="clarify",
    )

    assert guard_metric(failed, SALES_QUESTION) is failed
    assert guard_metric(clarify, SALES_QUESTION) is clarify


# --------------------------------------------------------------------------- #
# Офлайн-правила: регрессия на вопрос из дефекта
# --------------------------------------------------------------------------- #
def test_rules_plan_sales_question_by_revenue() -> None:
    """Правила на вопросе из дефекта дают выручку (это и было верное поведение)."""
    decision = RuleBasedPlanner().plan(SALES_QUESTION, today=TODAY)

    assert decision.intent == "top_bottom_products"
    assert decision.params["metric"] == "revenue"
    assert decision.params["limit"] == 10


# --------------------------------------------------------------------------- #
# Диспетчер: обычный путь
# --------------------------------------------------------------------------- #
def test_dispatcher_fixes_llm_quantity_answer(
    monkeypatch: pytest.MonkeyPatch, sales_frame: pd.DataFrame
) -> None:
    """Решение LLM с metric=quantity исполняется по выручке, и сводка это говорит."""
    decision = _decision(metric="quantity", limit=2, ascending=False)
    monkeypatch.setattr(
        "onec_analyst.dispatcher.select_planner", lambda *a, **k: _FakePlanner(decision)
    )

    outcome = process_request(
        SALES_QUESTION, True, ConversationState(), sales_frame, PROFILE
    )

    assert outcome.ok is True
    assert outcome.params["metric"] == "revenue"
    assert outcome.dsl["params"]["metric"] == "revenue"
    assert outcome.executed_params["metric"] == "revenue"
    assert "по выручке" in outcome.summary
    assert outcome.result is not None
    assert "revenue" in outcome.result.columns


def test_dispatcher_keeps_explicit_quantity(
    monkeypatch: pytest.MonkeyPatch, sales_frame: pd.DataFrame
) -> None:
    """«По количеству» из вопроса остаётся количеством и считается в штуках."""
    decision = _decision(metric="quantity", limit=2, ascending=False)
    monkeypatch.setattr(
        "onec_analyst.dispatcher.select_planner", lambda *a, **k: _FakePlanner(decision)
    )

    outcome = process_request(
        "Топ-2 товара по количеству за 2024",
        True,
        ConversationState(),
        sales_frame,
        PROFILE,
    )

    assert outcome.params["metric"] == "quantity"
    assert "по количеству" in outcome.summary
    assert outcome.result is not None
    assert "quantity" in outcome.result.columns


# --------------------------------------------------------------------------- #
# Диспетчер: агентный путь (граф подменён, сети нет)
# --------------------------------------------------------------------------- #
def test_agent_path_decision_is_guarded(
    monkeypatch: pytest.MonkeyPatch, sales_frame: pd.DataFrame
) -> None:
    """Агентный путь идёт через тот же guard: граф вернул quantity — считаем выручку."""
    decision = _decision(metric="quantity", limit=2, ascending=False)

    def fake_graph(state, **kwargs):
        return {"decision": decision, "trace": ["plan"], "llm_success": True}

    monkeypatch.setattr(
        "onec_analyst.dispatcher.select_planner",
        lambda *a, **k: LLMPlanner(_configured_settings()),
    )
    monkeypatch.setattr("onec_analyst.agent.graph.run_agent_graph", fake_graph)

    outcome = process_request(
        SALES_QUESTION, True, ConversationState(), sales_frame, PROFILE
    )

    assert outcome.ok is True
    assert outcome.params["metric"] == "revenue"
    assert outcome.dsl["params"]["metric"] == "revenue"
    assert "по выручке" in outcome.summary
    assert outcome.llm_success is True


# --------------------------------------------------------------------------- #
# Промпты: правило записано в обоих (JSON-DSL и tool calling)
# --------------------------------------------------------------------------- #
def test_system_prompt_forbids_quantity_without_request() -> None:
    """В системном промпте JSON-DSL есть жёсткое правило про metric."""
    prompt = SYSTEM_PROMPT.lower()

    assert 'metric="quantity" — только' in prompt
    assert 'metric="revenue"' in prompt
    assert "а не количество штук" in prompt


def test_agent_prompt_forbids_quantity_without_request() -> None:
    """В промпте агента (tool calling) — то же правило."""
    prompt = build_agent_system_prompt(today=date(2024, 5, 15))

    assert 'metric="quantity" — ТОЛЬКО' in prompt
    assert 'metric="revenue"' in prompt
