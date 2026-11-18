"""Тесты памяти диалога ``ConversationState``."""

from __future__ import annotations

from onec_analyst.llm_agent import AgentDecision
from onec_analyst.session import ConversationState


def _decision(dsl: dict | None = None, intent: str = "total") -> AgentDecision:
    return AgentDecision(
        ok=True,
        intent=intent,
        params={"start_date": "2024-01-01"},
        dsl=dsl if dsl is not None else {"intent": intent},
        source="rules",
    )


def test_new_state_is_empty() -> None:
    state = ConversationState()

    assert state.previous_dsl is None
    assert state.result_entities == []
    assert state.clarification_rounds == 0
    assert state.trace == []


def test_record_result_stores_dsl_and_entities() -> None:
    state = ConversationState()
    dsl = {"intent": "top_bottom_products", "params": {"limit": 5}}

    state.record_result(_decision(dsl, "top_bottom_products"), ["P1", "P2"])

    assert state.previous_dsl == dsl
    assert state.result_entities == ["P1", "P2"]
    # Копия, а не ссылка на исходный словарь решения.
    assert state.previous_dsl is not dsl


def test_record_result_accepts_empty_entities_and_missing_dsl() -> None:
    state = ConversationState()

    state.record_result(AgentDecision(ok=True, intent="total"))

    assert state.previous_dsl == {}
    assert state.result_entities == []


def test_record_trace_appends_and_ignores_blank_steps() -> None:
    state = ConversationState()

    state.record_trace("plan")
    state.record_trace("guard")
    state.record_trace("  ")
    state.record_trace("")

    assert state.trace == ["plan", "guard"]


def test_reset_clears_all_fields() -> None:
    state = ConversationState(
        previous_dsl={"intent": "total"},
        result_entities=["P1"],
        clarification_rounds=2,
        trace=["plan"],
    )

    state.reset()

    assert state.previous_dsl is None
    assert state.result_entities == []
    assert state.clarification_rounds == 0
    assert state.trace == []


def test_state_does_not_store_dataframes() -> None:
    """Память диалога не тянет pandas/Streamlit: только DSL и сущности."""
    state = ConversationState()
    state.record_result(_decision(), ["P1"])

    assert all(not hasattr(value, "columns") for value in vars(state).values())
