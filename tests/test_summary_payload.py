"""Snapshot-тесты текстовой сводки и структурированного payload.

Тексты зафиксированы после этапа 1.5: к сводке добавлен период, числа и
формулировки не менялись. Числа payload сверяются с DataFrame, а не с
отформатированной строкой.
"""

from __future__ import annotations

import pandas as pd
import pytest

from onec_analyst.analytics import dispatch_intent
from onec_analyst.llm_agent import AgentDecision
from onec_analyst.summary import (
    SummaryPayload,
    build_summary,
    build_summary_payload,
)
from tests.conftest import make_frame

DATE_PAIR_2023_2024 = (
    ("2023-01-01", "2023-12-31"),
    ("2024-01-01", "2024-12-31"),
)


def _decision(intent: str, **params: object) -> AgentDecision:
    return AgentDecision(ok=True, intent=intent, params=dict(params), source="rules")


def _result(intent: str, df: pd.DataFrame, **params: object) -> pd.DataFrame:
    return dispatch_intent(intent, df, dict(params))


#: 8 контрольных вопросов: текст должен совпадать байт-в-байт с прежним.
SNAPSHOTS = [
    (
        "total",
        "total",
        {"start_date": "2024-01-01", "end_date": "2024-12-31"},
        "За 2024 год: Выручка без НДС: 180,00 ₽; количество: 37 шт.; строк в выборке: 3; "
        "строк без даты: 0.",
    ),
    (
        "top",
        "top_bottom_products",
        {
            "metric": "revenue",
            "limit": 5,
            "ascending": False,
            "start_date": "2024-01-01",
            "end_date": "2024-12-31",
        },
        "За 2024 год: Лидер: «P1» — 120,00 ₽ (66,67 % от суммы). В выборке 3 товар(ов) по выручке.",
    ),
    (
        "antitop",
        "top_bottom_products",
        {
            "metric": "quantity",
            "limit": 3,
            "ascending": True,
            "start_date": "2023-01-01",
            "end_date": "2023-12-31",
        },
        "За 2023 год: Худший: «P3» — 3 шт. (9,09 % от суммы). В выборке 3 товар(ов) по количеству.",
    ),
    (
        "timeseries",
        "timeseries",
        {
            "freq": "M",
            "metric": "revenue",
            "start_date": "2023-01-01",
            "end_date": "2023-12-31",
        },
        "За 2023 год: 12 интервал(ов) по выручке, суммарно 330,00 ₽. Первый интервал: 100,00 ₽, "
        "последний: 0,00 ₽, максимум: 200,00 ₽ (2023-02).",
    ),
    (
        "dynamics",
        "counterparty_dynamics",
        {
            "period1": DATE_PAIR_2023_2024[0],
            "period2": DATE_PAIR_2023_2024[1],
            "metric": "revenue",
        },
        "2023 → 2024: Сравнение по 3 значени(ям): рост — 1, падение — 2, новых — 0, выпало — 0. "
        "Наибольший рост: «A» (20,00 ₽, рост); наибольшее падение: «B» (-160,00 ₽). "
        "Показатель — выручка.",
    ),
    (
        "group",
        "group_dynamics",
        {
            "period1": DATE_PAIR_2023_2024[0],
            "period2": DATE_PAIR_2023_2024[1],
            "metric": "revenue",
        },
        "2023 → 2024: Сравнение по 3 значени(ям): рост — 1, падение — 2, новых — 0, выпало — 0. "
        "Наибольший рост: «G1» (20,00 ₽, рост); наибольшее падение: «G2» (-160,00 ₽). "
        "Показатель — выручка.",
    ),
    (
        "total_filtered",
        "total",
        {
            "start_date": "2024-01-01",
            "end_date": "2024-12-31",
            "product_filter": "P1",
        },
        "По фильтру «P1» найдено 1 записей. За 2024 год: Выручка без НДС: 120,00 ₽; "
        "количество: 5 шт.; строк в выборке: 1; строк без даты: 0.",
    ),
    (
        "top_quantity",
        "top_bottom_products",
        {
            "metric": "quantity",
            "limit": 2,
            "start_date": "2024-01-01",
            "end_date": "2024-12-31",
        },
        "За 2024 год: Лидер: «P2» — 30 шт. (81,08 % от суммы). В выборке 2 товар(ов) по количеству.",
    ),
    (
        "compare",
        "compare_top_products",
        {
            "period1": DATE_PAIR_2023_2024[0],
            "period2": DATE_PAIR_2023_2024[1],
            "top_n": 5,
            "metric": "revenue",
        },
        "2023 → 2024: Сравнение топ-5: удержали место — 1, поднялись — 1, опустились — 1, "
        "вошли в топ — 0, выпали — 0. Наибольший рост: «P1» (+20,00 ₽ — "
        "100,00 → 120,00 ₽). Показатель — выручка.",
    ),
]


@pytest.mark.parametrize(
    ("name", "intent", "params", "expected"),
    SNAPSHOTS,
    ids=[item[0] for item in SNAPSHOTS],
)
def test_summary_text_is_unchanged(
    sales_frame: pd.DataFrame,
    name: str,
    intent: str,
    params: dict[str, object],
    expected: str,
) -> None:
    decision = _decision(intent, **params)
    result = _result(intent, sales_frame, **params)

    assert build_summary(decision, result, {"date_min": "2023-01-15"}) == expected


def test_negative_revenue_summary_is_unchanged() -> None:
    df = make_frame(
        [
            ("2024-01-10", "P1", "G1", "A", 5, 100.0),
            ("2024-02-10", "P2", "G2", "B", 2, -40.0),
            ("2024-03-10", "P2", "G2", "C", 1, -10.0),
        ]
    )
    params = {
        "start_date": "2024-01-01",
        "end_date": "2024-12-31",
        "group_by": "product",
    }
    decision = _decision("negative_revenue", **params)
    result = _result("negative_revenue", df, **params)

    assert build_summary(decision, result) == (
        "За 2024 год: Найдено 1 товаров с отрицательной выручкой. "
        "Общая сумма возвратов: -50,00 ₽ (2 строк)."
    )


# --------------------------------------------------------------------------- #
# SummaryPayload: текст — тот же, числа — из DataFrame
# --------------------------------------------------------------------------- #
def test_payload_is_frozen_dataclass() -> None:
    payload = build_summary_payload(
        _decision("total", start_date="2024-01-01", end_date="2024-12-31"),
        pd.DataFrame(),
    )

    assert isinstance(payload, SummaryPayload)
    with pytest.raises(Exception):
        payload.text = "другое"  # type: ignore[misc]


def test_total_payload_has_revenue_quantity_and_period(
    sales_frame: pd.DataFrame,
) -> None:
    params = {"start_date": "2024-01-01", "end_date": "2024-12-31"}
    decision = _decision("total", **params)
    result = _result("total", sales_frame, **params)

    payload = build_summary_payload(decision, result)

    assert payload.intent == "total"
    assert payload.params == params
    assert payload.periods == ("2024",)
    assert payload.entities == ()
    values = {(row["metric"], row["period"]): row["value"] for row in payload.values}
    assert values[("revenue", "2024")] == pytest.approx(180.0)
    assert values[("quantity", "2024")] == pytest.approx(37.0)
    assert all(row["item"] == "Итого" for row in payload.values)


def test_top_payload_contains_leader_and_entities(
    sales_frame: pd.DataFrame,
) -> None:
    params = {
        "metric": "revenue",
        "limit": 5,
        "start_date": "2024-01-01",
        "end_date": "2024-12-31",
    }
    decision = _decision("top_bottom_products", **params)
    result = _result("top_bottom_products", sales_frame, **params)

    payload = build_summary_payload(decision, result)

    leader = payload.values[0]
    assert leader["item"] == "P1"
    assert leader["metric"] == "revenue"
    assert leader["period"] == "2024"
    assert leader["value"] == pytest.approx(120.0)
    assert payload.entities == ("P1", "P2", "P3")


def test_timeseries_payload_has_sum_and_peak(sales_frame: pd.DataFrame) -> None:
    params = {
        "freq": "M",
        "metric": "revenue",
        "start_date": "2023-01-01",
        "end_date": "2023-12-31",
    }
    decision = _decision("timeseries", **params)
    result = _result("timeseries", sales_frame, **params)

    payload = build_summary_payload(decision, result)

    by_item = {row["item"]: row for row in payload.values}
    assert by_item["Сумма"]["value"] == pytest.approx(330.0)
    assert by_item["Сумма"]["period"] == "2023"
    assert by_item["Пик"]["value"] == pytest.approx(200.0)
    assert by_item["Пик"]["period"] == "2023-02"
    assert payload.periods == ("2023",)


def test_dynamics_payload_has_best_and_worst(sales_frame: pd.DataFrame) -> None:
    params = {
        "period1": DATE_PAIR_2023_2024[0],
        "period2": DATE_PAIR_2023_2024[1],
        "metric": "revenue",
    }
    decision = _decision("counterparty_dynamics", **params)
    result = _result("counterparty_dynamics", sales_frame, **params)

    payload = build_summary_payload(decision, result)

    values = {row["item"]: row["value"] for row in payload.values}
    assert values["A"] == pytest.approx(20.0)
    assert values["B"] == pytest.approx(-160.0)
    assert payload.periods == ("2023", "2024")
    assert payload.entities == ("B", "A", "C")


def test_compare_payload_has_value_per_product(sales_frame: pd.DataFrame) -> None:
    params = {
        "period1": DATE_PAIR_2023_2024[0],
        "period2": DATE_PAIR_2023_2024[1],
        "top_n": 5,
        "metric": "revenue",
    }
    decision = _decision("compare_top_products", **params)
    result = _result("compare_top_products", sales_frame, **params)

    payload = build_summary_payload(decision, result)

    values = {row["item"]: row["value"] for row in payload.values}
    assert values == {
        "P1": pytest.approx(120.0),
        "P2": pytest.approx(40.0),
        "P3": pytest.approx(20.0),
    }
    assert payload.periods == ("2023", "2024")


def test_negative_payload_totals_returns() -> None:
    df = make_frame(
        [
            ("2024-01-10", "P1", "G1", "A", 5, 100.0),
            ("2024-02-10", "P2", "G2", "B", 2, -40.0),
            ("2024-03-10", "P2", "G2", "C", 1, -10.0),
        ]
    )
    params = {
        "start_date": "2024-01-01",
        "end_date": "2024-12-31",
        "group_by": "product",
    }
    payload = build_summary_payload(
        _decision("negative_revenue", **params),
        _result("negative_revenue", df, **params),
    )

    assert payload.values[0]["item"] == "Возвраты"
    assert payload.values[0]["value"] == pytest.approx(-50.0)
    assert payload.entities == ("P2",)


def test_empty_result_has_no_values(sales_frame: pd.DataFrame) -> None:
    params = {"start_date": "1990-01-01", "end_date": "1990-12-31"}
    payload = build_summary_payload(
        _decision("top_bottom_products", **params),
        _result("top_bottom_products", sales_frame, **params),
        {"date_min": "2023-01-15", "date_max": "2024-03-05"},
    )

    assert payload.values == ()
    assert payload.entities == ()
    assert "За 1990 год: данных по выручке нет" in payload.text
    assert payload.periods == ("1990",)


def test_month_and_quarter_periods_are_human_readable(
    sales_frame: pd.DataFrame,
) -> None:
    params = {"start_date": "2024-01-01", "end_date": "2024-03-31"}
    payload = build_summary_payload(
        _decision("total", **params),
        _result("total", sales_frame, **params),
    )

    assert payload.periods == ("2024-Q1",)
    assert payload.values[0]["period"] == "2024-Q1"

    month_params = {"start_date": "2024-03-01", "end_date": "2024-03-31"}
    month_payload = build_summary_payload(
        _decision("total", **month_params),
        _result("total", sales_frame, **month_params),
    )

    assert month_payload.periods == ("март 2024",)
    assert month_payload.values[0]["period"] == "2024-03"
