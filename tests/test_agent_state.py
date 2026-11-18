"""Состояние агента, статусы и аддитивность ``AgentDecision``."""

from __future__ import annotations

import json
from datetime import date

import pytest

from onec_analyst.agent.state import (
    AgentState,
    CallBudgetGuard,
    DecisionStatus,
    MAX_CLARIFICATIONS,
    MAX_LLM_CALLS,
    UnlimitedGuard,
    initial_state,
    mark_step,
    state_today,
    status_of,
)
from onec_analyst.llm_agent import AgentDecision, RuleBasedPlanner


def test_decision_status_covers_all_five_states() -> None:
    """Статусы из ТЗ 6.4 §2.2 различаются и сериализуются строкой."""
    values = [status.value for status in DecisionStatus]

    assert values == ["ok", "clarify", "refuse", "error", "fallback"]
    assert DecisionStatus("refuse") is DecisionStatus.REFUSE


def test_agent_decision_is_additive() -> None:
    """Старый конструктор без status/refusal_reason работает как раньше."""
    decision = AgentDecision(ok=True, intent="total", params={"start_date": "2024-01-01"})

    assert decision.status == "ok"
    assert decision.refusal_reason is None
    assert decision.trace == ()
    assert decision.ok is True
    assert decision.intent == "total"


def test_agent_decision_accepts_new_fields() -> None:
    """Новые поля можно задать явно, не меняя остальной контракт."""
    decision = AgentDecision(
        ok=False,
        status="refuse",
        refusal_reason="Средний чек не поддерживается.",
    )

    assert decision.status == "refuse"
    assert decision.refusal_reason == "Средний чек не поддерживается."
    assert decision.params == {}
    assert decision.dsl == {}


def test_rules_clarification_is_marked_clarify() -> None:
    """Офлайн-правила выставляют status=clarify для уточняющего вопроса."""
    decision = RuleBasedPlanner().plan("Сравни продажи")

    assert decision.intent == "clarification"
    assert decision.status == "clarify"
    assert decision.ok is True


def test_status_of_handles_missing_and_unknown() -> None:
    """Нет решения или неизвестный статус → error, а не исключение."""
    assert status_of(None) is DecisionStatus.ERROR
    assert status_of(AgentDecision(ok=True)) is DecisionStatus.OK
    assert status_of(AgentDecision(ok=False, status="что-то")) is DecisionStatus.ERROR


def test_initial_state_fills_domain_fields() -> None:
    """Начальное состояние соответствует ТЗ 6.4 §2.1."""
    state = initial_state(
        "  Сколько мы продали за 2024 год?  ",
        previous_dsl={"intent": "total", "params": {}},
        result_entities=["P1"],
        today=date(2026, 11, 18),
        clarification_rounds=1,
    )

    assert state["question"] == "Сколько мы продали за 2024 год?"
    assert state["resolved_question"] == state["question"]
    assert state["previous_dsl"] == {"intent": "total", "params": {}}
    assert state["result_entities"] == ["P1"]
    assert state["today"] == "2026-11-18"
    assert state["decision"] is None
    assert state["validation_error"] is None
    assert state["retry_count"] == 0
    assert state["clarification_rounds"] == 1
    assert state["trace"] == []
    assert state["error"] is None
    assert state["llm_success"] is False


def test_initial_state_is_json_serializable() -> None:
    """Доменная часть состояния сериализуется (для логов/аудита этапа 3)."""
    state = initial_state("Итог за 2024", today=date(2026, 11, 18))
    domain = {
        key: state[key]
        for key in AgentState.__annotations__
        if key not in ("client", "planner", "guard", "decision", "inherited")
    }

    payload = json.dumps(domain, ensure_ascii=False)

    assert "Итог за 2024" in payload
    assert "2026-11-18" in payload


def test_state_today_parses_and_falls_back() -> None:
    """Дата из состояния; битая строка не роняет узел."""
    assert state_today({"today": "2026-11-18"}) == date(2026, 11, 18)
    assert state_today({"today": "not-a-date"}) == date.today()
    assert state_today({}) == date.today()


def test_call_budget_guard_counts_only_success() -> None:
    """Guard разрешает ровно три вызова и считает успешные."""
    guard = CallBudgetGuard()

    assert guard.before_call() is True
    guard.after_success()
    assert guard.before_call() is True
    guard.after_success()
    assert guard.before_call() is True
    guard.after_success()

    assert guard.used == MAX_LLM_CALLS
    assert guard.before_call() is False


def test_unlimited_guard_always_allows() -> None:
    """Guard по умолчанию не ограничивает (лимит живёт в UI)."""
    guard = UnlimitedGuard()

    for _ in range(10):
        assert guard.before_call() is True
        guard.after_success()


def test_mark_step_returns_new_trace() -> None:
    """Трассировка не мутируется: узлы возвращают новые списки."""
    state = initial_state("вопрос")
    first = mark_step(state, "agent_decision")
    second = mark_step({**state, "trace": first}, "validate_dsl")

    assert first == ["agent_decision"]
    assert second == ["agent_decision", "validate_dsl"]
    assert state["trace"] == []


def test_max_clarifications_is_two() -> None:
    """Лимит уточнений совпадает с UI (два круга)."""
    assert MAX_CLARIFICATIONS == 2


@pytest.mark.parametrize("status", list(DecisionStatus))
def test_status_roundtrip(status: DecisionStatus) -> None:
    """Статус восстанавливается из строки и обратно."""
    decision = AgentDecision(ok=status is DecisionStatus.OK, status=status.value)

    assert status_of(decision) is status
