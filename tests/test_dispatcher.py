"""Тесты диспетчера: офлайн-ответ, уточнение, ошибка, защита и трассировка."""

from __future__ import annotations

import pandas as pd
import pytest

from onec_analyst.clarification import CLARIFICATION_LIMIT_ERROR
from onec_analyst.dispatcher import (
    AssistantResult,
    DispatchError,
    process_request,
    select_planner,
)
from onec_analyst.llm_agent import (
    AgentDecision,
    LLMPlanner,
    LLMSettings,
    RuleBasedPlanner,
)
from onec_analyst.session import ConversationState

PROFILE = {"date_min": "2023-01-15", "date_max": "2024-03-05"}


def _configured_settings() -> LLMSettings:
    return LLMSettings(provider="deepseek", model="deepseek-chat", api_key="test-key")


class _FakePlanner:
    """Планировщик-заглушка: возвращает заранее заданное решение."""

    def __init__(self, decision: AgentDecision) -> None:
        self.decision = decision
        self.calls: list[tuple] = []

    def plan(self, question, history=None, data_profile=None):
        self.calls.append((question, history, data_profile))
        return self.decision


def _install(monkeypatch: pytest.MonkeyPatch, decision: AgentDecision) -> _FakePlanner:
    planner = _FakePlanner(decision)
    monkeypatch.setattr("onec_analyst.dispatcher.select_planner", lambda *a, **k: planner)
    return planner


# --------------------------------------------------------------------------- #
# Выбор планировщика
# --------------------------------------------------------------------------- #
def test_select_planner_uses_llm_when_enabled_and_configured() -> None:
    assert isinstance(select_planner(True, _configured_settings()), LLMPlanner)


def test_select_planner_falls_back_to_rules() -> None:
    assert isinstance(select_planner(False, _configured_settings()), RuleBasedPlanner)
    assert isinstance(select_planner(True, LLMSettings(provider="none")), RuleBasedPlanner)


# --------------------------------------------------------------------------- #
# Офлайн-ответ
# --------------------------------------------------------------------------- #
def test_process_request_ok_offline(sales_frame: pd.DataFrame) -> None:
    session = ConversationState()

    outcome = process_request(
        "Итог за 2024", False, session, sales_frame, PROFILE
    )

    assert isinstance(outcome, AssistantResult)
    assert outcome.ok is True
    assert outcome.intent == "total"
    assert outcome.result is not None
    assert outcome.result.iloc[0]["revenue"] == pytest.approx(180.0)
    assert "180,00 ₽" in outcome.summary
    assert outcome.payload is not None
    assert outcome.payload.text == outcome.summary
    assert outcome.source == "rules"
    assert outcome.llm_success is False
    assert outcome.trace == ("plan", "guard", "dispatch", "summary")
    assert session.previous_dsl == outcome.dsl
    assert session.result_entities == []
    assert session.trace == ["plan", "guard", "dispatch", "summary"]


def test_process_request_updates_entities(sales_frame: pd.DataFrame) -> None:
    session = ConversationState()

    outcome = process_request(
        "Топ-3 товара по выручке за 2024", False, session, sales_frame, PROFILE
    )

    assert outcome.intent == "top_bottom_products"
    assert session.result_entities == ["P1", "P2", "P3"]


def test_process_request_clarification_does_not_calculate(
    sales_frame: pd.DataFrame,
) -> None:
    session = ConversationState()

    outcome = process_request(
        "Покажи что-нибудь интересное", False, session, sales_frame, PROFILE
    )

    assert outcome.ok is True
    assert outcome.intent == "clarification"
    assert outcome.clarification is not None
    assert outcome.result is None
    assert outcome.summary == ""
    assert outcome.payload is None
    assert outcome.trace == ("plan", "guard")
    assert session.previous_dsl is None


def test_process_request_offline_force_after_clarification_limit(
    monkeypatch: pytest.MonkeyPatch, sales_frame: pd.DataFrame
) -> None:
    """OFF: после лимита уточнений — force_interpretation через process_request."""
    clarification = AgentDecision(
        ok=True,
        intent="clarification",
        clarification=None,
        status="clarify",
    )
    _install(monkeypatch, clarification)

    def fake_force(question: str, **kwargs):  # type: ignore[no-untyped-def]
        return RuleBasedPlanner().plan("Итог за 2024", data_profile=PROFILE)

    monkeypatch.setattr("onec_analyst.dispatcher.force_interpretation", fake_force)
    session = ConversationState()
    session.clarification_rounds = 2

    outcome = process_request("продажи", False, session, sales_frame, PROFILE)

    assert outcome.ok is True
    assert outcome.status == "ok"
    assert outcome.intent == "total"
    assert outcome.result is not None
    assert outcome.degraded is True
    assert outcome.clarification_limit_applied is True


def test_process_request_offline_clarification_limit_honest_refusal(
    sales_frame: pd.DataFrame,
) -> None:
    """OFF: лимит исчерпан, правила не выбрали интент — уровень 3 ADR 0007."""
    session = ConversationState()
    session.clarification_rounds = 2

    outcome = process_request(
        "Покажи что-нибудь интересное", False, session, sales_frame, PROFILE
    )

    assert outcome.ok is False
    assert outcome.status == "error"
    assert outcome.error == CLARIFICATION_LIMIT_ERROR
    assert outcome.clarification_limit_applied is True
    assert outcome.result is None


def test_process_request_error_returns_without_calculation(
    sales_frame: pd.DataFrame,
) -> None:
    session = ConversationState()

    outcome = process_request("", False, session, sales_frame, PROFILE)

    assert outcome.ok is False
    assert outcome.error == "Пустой вопрос"
    assert outcome.result is None
    assert outcome.trace == ("plan", "guard")


# --------------------------------------------------------------------------- #
# Защита и ошибки расчёта
# --------------------------------------------------------------------------- #
def test_guard_replaces_antipot_and_keeps_llm_success(
    monkeypatch: pytest.MonkeyPatch, sales_frame: pd.DataFrame
) -> None:
    llm_decision = AgentDecision(
        ok=True,
        intent="top_bottom_products",
        params={"limit": 5, "ascending": True},
        dsl={"intent": "top_bottom_products"},
        source="llm",
    )
    _install(monkeypatch, llm_decision)

    outcome = process_request(
        "покажи товары с отрицательной выручкой за 2024",
        True,
        ConversationState(),
        sales_frame,
        PROFILE,
        settings=_configured_settings(),
    )

    assert outcome.intent == "negative_revenue"
    assert outcome.source == "rules"
    assert outcome.llm_success is True


def test_dispatch_error_carries_decision(
    monkeypatch: pytest.MonkeyPatch, sales_frame: pd.DataFrame
) -> None:
    broken = AgentDecision(
        ok=True,
        intent="top_bottom_products",
        params={"limit": 0},
        dsl={"intent": "top_bottom_products", "params": {"limit": 0}},
        source="rules",
    )
    _install(monkeypatch, broken)
    session = ConversationState()

    with pytest.raises(DispatchError) as excinfo:
        process_request("топ", False, session, sales_frame, PROFILE)

    assert excinfo.value.decision is broken
    assert isinstance(excinfo.value.original, ValueError)
    assert "limit" in str(excinfo.value.original)
    assert session.previous_dsl is None
    assert session.trace == ["plan", "guard"]


def test_history_is_passed_to_planner(
    monkeypatch: pytest.MonkeyPatch, sales_frame: pd.DataFrame
) -> None:
    decision = AgentDecision(ok=True, intent="total", params={}, source="rules")
    planner = _install(monkeypatch, decision)
    history = [{"role": "user", "content": "прошлый вопрос"}]

    process_request(
        "Итог за 2024", False, ConversationState(), sales_frame, PROFILE, history=history
    )

    assert planner.calls[0][1] == history


# --------------------------------------------------------------------------- #
# Совместимость AssistantResult
# --------------------------------------------------------------------------- #
def test_assistant_result_as_decision_roundtrips() -> None:
    result = AssistantResult(
        ok=True,
        intent="total",
        params={"start_date": "2024-01-01"},
        dsl={"intent": "total"},
        summary="сводка",
        source="rules",
        raw_response="raw",
    )

    decision = result.as_decision()

    assert decision.ok is True
    assert decision.intent == "total"
    assert decision.params == {"start_date": "2024-01-01"}
    assert decision.dsl == {"intent": "total"}
    assert decision.raw_response == "raw"
