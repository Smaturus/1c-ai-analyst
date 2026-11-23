"""Тесты исполнения DSL: валидация, 7 интентов, сущности и ошибки."""

from __future__ import annotations

import pandas as pd
import pytest

from onec_analyst.analytics import dispatch_intent
from onec_analyst.llm_agent import AgentDecision
from onec_analyst.pipeline import (
    ExecutionResult,
    execute,
    extract_entities,
    normalize_dsl,
    validate_decision,
)
from onec_analyst.summary import build_summary
from tests.conftest import make_frame

PERIODS = {
    "period1": ("2023-01-01", "2023-12-31"),
    "period2": ("2024-01-01", "2024-12-31"),
}

INTENT_PARAMS = {
    "total": {"start_date": "2024-01-01", "end_date": "2024-12-31"},
    "top_bottom_products": {
        "metric": "revenue",
        "limit": 3,
        "start_date": "2024-01-01",
        "end_date": "2024-12-31",
    },
    "timeseries": {
        "freq": "M",
        "metric": "revenue",
        "start_date": "2023-01-01",
        "end_date": "2023-12-31",
    },
    "negative_revenue": {
        "start_date": "2024-01-01",
        "end_date": "2024-12-31",
        "group_by": "product",
    },
    "compare_top_products": {**PERIODS, "top_n": 5, "metric": "revenue"},
    "counterparty_dynamics": {**PERIODS, "metric": "revenue"},
    "group_dynamics": {**PERIODS, "metric": "revenue"},
}


def _decision(intent: str, **params: object) -> AgentDecision:
    return AgentDecision(ok=True, intent=intent, params=dict(params), source="rules")


@pytest.mark.parametrize("intent", sorted(INTENT_PARAMS))
def test_execute_all_seven_intents(sales_frame: pd.DataFrame, intent: str) -> None:
    params = INTENT_PARAMS[intent]
    decision = _decision(intent, **params)
    frame = pd.concat(
        [sales_frame, make_frame([("2024-04-01", "P4", "G4", "D", 1, -5.0)])],
        ignore_index=True,
    )

    execution = execute(decision, frame, {"date_min": "2023-01-15"})

    assert isinstance(execution, ExecutionResult)
    assert execution.decision is decision
    assert isinstance(execution.result, pd.DataFrame)
    assert not execution.result.empty, intent
    assert execution.summary_payload.intent == intent
    assert execution.summary_payload.text == build_summary(decision, execution.result)
    assert execution.summary == execution.summary_payload.text


def test_execute_negative_revenue_with_returns() -> None:
    df = make_frame(
        [
            ("2024-01-10", "P1", "G1", "A", 5, 100.0),
            ("2024-02-10", "P2", "G2", "B", 2, -40.0),
        ]
    )
    decision = _decision("negative_revenue", group_by="product")

    execution = execute(decision, df)

    assert execution.result["revenue"].sum() == pytest.approx(-40.0)
    assert execution.summary_payload.values[0]["value"] == pytest.approx(-40.0)


def test_execute_does_not_swallow_dispatch_errors(sales_frame: pd.DataFrame) -> None:
    decision = _decision("top_bottom_products", limit=0)

    with pytest.raises(ValueError):
        execute(decision, sales_frame)


def test_execute_rejects_clarification_and_failed_decision(
    sales_frame: pd.DataFrame,
) -> None:
    with pytest.raises(ValueError):
        execute(AgentDecision(ok=True, intent="clarification"), sales_frame)

    with pytest.raises(ValueError):
        execute(AgentDecision(ok=False, error="нет DSL"), sales_frame)


# --------------------------------------------------------------------------- #
# validate_decision / normalize_dsl
# --------------------------------------------------------------------------- #
def test_validate_decision_builds_agent_decision() -> None:
    dsl = {
        "intent": "total",
        "params": {"start_date": "2024-01-01", "end_date": "2024-12-31"},
    }

    decision = validate_decision(dsl)

    assert decision.ok is True
    assert decision.intent == "total"
    assert decision.params["start_date"].isoformat() == "2024-01-01"
    assert decision.dsl["intent"] == "total"


def test_validate_decision_drops_empty_filters() -> None:
    """Пустой product_filter от модели — это «фильтра нет», а не ошибка dispatch."""
    dsl = {
        "intent": "timeseries",
        "params": {
            "freq": "M",
            "start_date": "2024-01-01",
            "end_date": "2024-12-31",
            "product_filter": "",
        },
    }

    decision = validate_decision(dsl)

    assert "product_filter" not in decision.params
    assert "product_filter" not in decision.dsl["params"]


def test_normalize_dsl_is_alias_for_validate_decision() -> None:
    dsl = {"intent": "total", "params": {}}

    assert normalize_dsl(dsl) == validate_decision(dsl)


def test_validate_decision_accepts_clarification() -> None:
    dsl = {
        "intent": "clarification",
        "question": "За какой период?",
        "missing": ["period"],
        "suggestions": ["2023 и 2024"],
    }

    decision = validate_decision(dsl)

    assert decision.intent == "clarification"
    assert decision.clarification is not None
    assert decision.clarification.question == "За какой период?"


def test_validate_decision_rejects_contract_violations() -> None:
    with pytest.raises(ValueError):
        validate_decision({"intent": "total", "params": {"direction": "top"}})


# --------------------------------------------------------------------------- #
# extract_entities
# --------------------------------------------------------------------------- #
def test_extract_entities_top_products(sales_frame: pd.DataFrame) -> None:
    decision = _decision("top_bottom_products", **INTENT_PARAMS["top_bottom_products"])
    result = dispatch_intent(decision.intent or "", sales_frame, decision.params)

    assert extract_entities(decision, result) == ["P1", "P2", "P3"]


def test_extract_entities_compare_products(sales_frame: pd.DataFrame) -> None:
    decision = _decision(
        "compare_top_products", **INTENT_PARAMS["compare_top_products"]
    )
    result = dispatch_intent(decision.intent or "", sales_frame, decision.params)

    assert extract_entities(decision, result) == ["P1", "P2", "P3"]


def test_extract_entities_negative_revenue_uses_group_by() -> None:
    df = make_frame(
        [
            ("2024-01-10", "P1", "G1", "A", 5, 100.0),
            ("2024-02-10", "P2", "G2", "B", 2, -40.0),
            ("2024-03-10", "P2", "G2", "C", 1, -10.0),
        ]
    )
    decision = _decision("negative_revenue", group_by="counterparty")
    result = dispatch_intent("negative_revenue", df, decision.params)

    assert extract_entities(decision, result) == ["B", "C"]


def test_extract_entities_dynamics_and_groups(sales_frame: pd.DataFrame) -> None:
    counterparties = _decision(
        "counterparty_dynamics", **INTENT_PARAMS["counterparty_dynamics"]
    )
    groups = _decision("group_dynamics", **INTENT_PARAMS["group_dynamics"])

    cp_result = dispatch_intent("counterparty_dynamics", sales_frame, counterparties.params)
    gr_result = dispatch_intent("group_dynamics", sales_frame, groups.params)

    assert extract_entities(counterparties, cp_result) == ["B", "A", "C"]
    assert extract_entities(groups, gr_result) == ["G2", "G1", "G3"]


@pytest.mark.parametrize("intent", ["timeseries", "total"])
def test_extract_entities_is_empty_for_scalar_intents(
    sales_frame: pd.DataFrame, intent: str
) -> None:
    decision = _decision(intent, **INTENT_PARAMS[intent])
    result = dispatch_intent(intent, sales_frame, decision.params)

    assert extract_entities(decision, result) == []


def test_extract_entities_handles_empty_result() -> None:
    decision = _decision("top_bottom_products")

    assert extract_entities(decision, pd.DataFrame()) == []
    assert extract_entities(decision, None) == []
