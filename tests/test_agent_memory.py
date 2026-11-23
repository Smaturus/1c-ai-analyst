"""Память диалога: кейсы GoldSet 009/016 (clarify period2), 024/048, 008/029."""

from __future__ import annotations

from datetime import date

from onec_analyst.agent.graph import run_agent_graph
from onec_analyst.agent.state import initial_state

from tests.agent_support import (
    FakeLLMClient,
    echo_bounds_response,
    iso,
    json_response,
    refuse_response,
)

TODAY = date(2026, 11, 18)


def _run(question: str, responses, *, previous_dsl=None, entities=None):
    """Запустить граф с памятью прошлого шага."""
    client = FakeLLMClient(responses)
    state = initial_state(
        question,
        previous_dsl=previous_dsl,
        result_entities=entities,
        today=TODAY,
        client=client,
    )
    final = run_agent_graph(state, client=client)
    return final, client


# --------------------------------------------------------------------------- #
# 009 / 016: один период у интента сравнения → clarify(period2)
# --------------------------------------------------------------------------- #
def test_009_group_dynamics_with_inherited_period_needs_period2() -> None:
    """«А по группам за тот же период?» — период восстановлен, period2 нет."""
    final, _ = _run(
        "А по группам за тот же период?",
        [json_response("group_dynamics", {"metric": "revenue"})],
        previous_dsl={
            "intent": "timeseries",
            "params": {"freq": "M", "start_date": "2023-01-01", "end_date": "2023-12-31"},
        },
    )

    decision = final["decision"]
    assert decision.status == "clarify"
    assert decision.clarification.missing == ["period2"]
    assert final["trace"][0] == "resolve_context"
    assert "2023-01-01" in final["resolved_question"]


def test_016_counterparty_dynamics_with_entities_needs_period2() -> None:
    """«А теперь покажи их по контрагентам» — сущности есть, period2 нет."""
    final, _ = _run(
        "А теперь покажи их по контрагентам",
        [json_response("counterparty_dynamics", {"metric": "revenue"})],
        previous_dsl={
            "intent": "top_bottom_products",
            "params": {"limit": 2, "start_date": "2024-01-01", "end_date": "2024-12-31"},
        },
        entities=["Колонка Му", "Клавиатура Lambda"],
    )

    decision = final["decision"]
    assert decision.status == "clarify"
    assert "period2" in decision.clarification.missing
    assert "Колонка Му" in final["resolved_question"]


# --------------------------------------------------------------------------- #
# 024 / 048: тот же период и смена гранулярности
# --------------------------------------------------------------------------- #
def test_024_top3_inherits_previous_period() -> None:
    """«А теперь покажи топ-3» наследует период прошлого итога."""
    final, _ = _run(
        "А теперь покажи топ-3",
        [echo_bounds_response("top_bottom_products", {"limit": 3, "ascending": False})],
        previous_dsl={
            "intent": "total",
            "params": {"start_date": "2023-01-01", "end_date": "2023-12-31"},
        },
    )

    decision = final["decision"]
    assert decision.status == "ok"
    assert decision.intent == "top_bottom_products"
    assert iso(decision.params["start_date"]) == "2023-01-01"
    assert iso(decision.params["end_date"]) == "2023-12-31"


def test_048_granularity_change_keeps_period() -> None:
    """«То же самое, но по неделям»: период сохранён, freq = W."""
    final, _ = _run(
        "А теперь то же самое, но по неделям",
        [echo_bounds_response("timeseries", {"freq": "W", "metric": "revenue"})],
        previous_dsl={
            "intent": "timeseries",
            "params": {"freq": "M", "start_date": "2023-01-01", "end_date": "2023-12-31"},
        },
    )

    decision = final["decision"]
    assert decision.status == "ok"
    assert decision.intent == "timeseries"
    assert decision.params["freq"] == "W"
    assert iso(decision.params["start_date"]) == "2023-01-01"


# --------------------------------------------------------------------------- #
# 008 / 029: неподдерживаемое сравнение набора товаров → refuse
# --------------------------------------------------------------------------- #
def test_008_compare_of_result_entities_is_refused() -> None:
    """Сравнение конкретных товаров из памяти — отказ с объяснением."""
    final, _ = _run(
        "А как они соотносятся с прошлым кварталом?",
        [
            json_response(
                "compare_top_products",
                {
                    "period1": ["2024-07-01", "2024-09-30"],
                    "period2": ["2024-10-01", "2024-12-31"],
                    "top_n": 2,
                },
            )
        ],
        previous_dsl={
            "intent": "top_bottom_products",
            "params": {"limit": 2, "start_date": "2024-10-01", "end_date": "2024-12-31"},
        },
        entities=["Колонка Му", "Клавиатура Lambda"],
    )

    decision = final["decision"]
    assert decision.status == "refuse"
    reason = decision.refusal_reason.lower()
    assert "не поддерживается" in reason
    assert "сравнен" in reason


def test_029_dynamics_of_result_entities_is_refused() -> None:
    """Динамика по произвольному набору товаров — отказ, а не частичный ряд."""
    final, _ = _run(
        "Покажи динамику этих товаров",
        [json_response("timeseries", {"freq": "M", "metric": "revenue"})],
        previous_dsl={
            "intent": "top_bottom_products",
            "params": {"limit": 2, "start_date": "2023-01-01", "end_date": "2023-12-31"},
        },
        entities=["Мышь Карра", "Клавиатура Lambda"],
    )

    decision = final["decision"]
    assert decision.status == "refuse"
    reason = decision.refusal_reason.lower()
    assert "не поддерживается" in reason
    assert "товар" in reason


def test_memory_is_not_used_without_previous_dsl() -> None:
    """Без previous_dsl контекст не выдумывается: вопрос идёт как есть."""
    final, _ = _run(
        "Покажи динамику этих товаров",
        [json_response("timeseries", {"freq": "M"})],
    )

    assert final["trace"][0] == "agent_decision"
    assert final["resolved_question"] == "Покажи динамику этих товаров"


def test_refusal_from_model_is_kept_without_policy_change() -> None:
    """Отказ модели с причиной не подменяется правилами."""
    final, _ = _run(
        "Сравни продажи Колонки Му за 2023 и 2024",
        [refuse_response("Сравнение продаж конкретного товара не поддерживается.")],
    )

    assert final["decision"].status == "refuse"
    assert "Сравнение продаж" in final["decision"].refusal_reason
