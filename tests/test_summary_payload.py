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
        "За 2023 год: 12 интервал(ов) по выручке, суммарно 330,00 ₽. В первом (2023-01): 100,00 ₽, "
        "в последнем (2023-12): 0,00 ₽, максимум: 200,00 ₽ (2023-02).",
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


def test_dynamics_without_growth_does_not_claim_growth() -> None:
    """Все дельты ≤ 0: сводка не называет падение «наибольшим ростом»."""
    result = pd.DataFrame(
        {
            "counterparty": ["A", "B"],
            "period1_value": [100.0, 50.0],
            "period2_value": [0.0, 0.0],
            "delta": [-100.0, -50.0],
            "delta_pct": [-100.0, -100.0],
            "direction": ["lost", "lost"],
        }
    )
    decision = _decision(
        "counterparty_dynamics",
        period1=DATE_PAIR_2023_2024[0],
        period2=DATE_PAIR_2023_2024[1],
        metric="revenue",
    )

    summary = build_summary(decision, result)

    assert "Наибольший рост" not in summary
    assert "Роста нет" in summary
    assert "«A» отсутствует в периоде 2 (в периоде 1 — 100,00 ₽)" in summary
    assert "падений нет" in summary


def test_dynamics_with_growth_names_best_growth() -> None:
    """Есть положительная дельта: «Наибольший рост» — именно растущий контрагент."""
    result = pd.DataFrame(
        {
            "counterparty": ["A", "B"],
            "period1_value": [100.0, 200.0],
            "period2_value": [120.0, 40.0],
            "delta": [20.0, -160.0],
            "delta_pct": [20.0, -80.0],
            "direction": ["up", "down"],
        }
    )
    decision = _decision(
        "counterparty_dynamics",
        period1=DATE_PAIR_2023_2024[0],
        period2=DATE_PAIR_2023_2024[1],
        metric="revenue",
    )

    summary = build_summary(decision, result)

    assert "Наибольший рост: «A» (20,00 ₽, рост)" in summary
    assert "наибольшее падение: «B» (-160,00 ₽)" in summary


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


def test_timeseries_single_interval_summary_has_no_first_last_peak() -> None:
    """Один интервал: без «в первом / в последнем / максимум» — там одно число.

    Дефект приёмки: на вопрос «Сколько мы продали штук в октябре 2023?» сводка
    трижды повторяла 435 шт. («в первом… в последнем… максимумом»).
    """
    decision = _decision(
        "timeseries",
        freq="M",
        metric="quantity",
        start_date="2023-10-01",
        end_date="2023-10-31",
    )
    result = pd.DataFrame(
        {"period": pd.to_datetime(["2023-10-01"]), "quantity": [435.0]}
    )

    text = build_summary(decision, result)

    assert text == "За октябрь 2023: 435 шт. по количеству (интервал 2023-10)."
    assert "первом" not in text.lower()
    assert "максимум" not in text


def test_timeseries_two_intervals_keep_first_last_peak() -> None:
    """Два интервала и больше — прежняя формулировка с экстремумами."""
    decision = _decision(
        "timeseries",
        freq="M",
        metric="quantity",
        start_date="2023-01-01",
        end_date="2023-02-28",
    )
    result = pd.DataFrame(
        {
            "period": pd.to_datetime(["2023-01-01", "2023-02-01"]),
            "quantity": [10.0, 20.0],
        }
    )

    text = build_summary(decision, result)
    # «шт.» + точка конца предложения дают «шт..» — это существующая мелочь
    # форматирования, здесь она не предмет проверки.
    normalized = text.replace("шт..", "шт.")

    assert "2 интервал(ов)" in normalized
    assert "В первом (2023-01): 10 шт." in normalized
    assert "максимум: 20 шт. (2023-02)" in normalized


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


def test_empty_filtered_result_names_filter_not_missing_data(
    sales_frame: pd.DataFrame,
) -> None:
    """Пустота из-за фильтра — не «данных по выручке нет» (DECISIONS #30).

    До правки текст был «За 2023 год: данных по выручке нет. В текущем источнике
    данные доступны с …» — при том что за 2023 год данные в источнике есть.
    """
    params = {
        "metric": "revenue",
        "limit": 5,
        "start_date": "2023-01-01",
        "end_date": "2023-12-31",
        "product_filter": "Кабель",
    }
    payload = build_summary_payload(
        _decision("top_bottom_products", **params),
        _result("top_bottom_products", sales_frame, **params),
        {"date_min": "2021-09-01", "date_max": "2026-01-20"},
    )

    assert payload.text.startswith("По фильтру «Кабель» за 2023 год")
    assert "данных по выручке нет" not in payload.text
    assert "01.09.2021" in payload.text
    assert payload.values == ()


def test_empty_result_without_filter_keeps_legacy_text(
    sales_frame: pd.DataFrame,
) -> None:
    """Без фильтра поведение прежнее: период пуст, диапазон источника в тексте."""
    params = {"start_date": "1990-01-01", "end_date": "1990-12-31"}
    payload = build_summary_payload(
        _decision("top_bottom_products", **params),
        _result("top_bottom_products", sales_frame, **params),
        {"date_min": "2023-01-15", "date_max": "2024-03-05"},
    )

    assert payload.text == (
        "За 1990 год: данных по выручке нет. "
        "В текущем источнике данные доступны с 15.01.2023 по 05.03.2024."
    )


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


def test_new_counterparty_is_not_ranked_as_growth() -> None:
    """База периода 1 = 0 («новый») не называется «наибольшим ростом» (DECISIONS)."""
    result = pd.DataFrame(
        {
            "counterparty": ["Дельта", "Альфа"],
            "period1_value": [0.0, 200.0],
            "period2_value": [450000.0, 35000.0],
            "delta": [450000.0, -165000.0],
            "delta_pct": [None, -82.5],
            "direction": ["new", "down"],
        }
    )
    decision = _decision(
        "counterparty_dynamics",
        period1=DATE_PAIR_2023_2024[0],
        period2=DATE_PAIR_2023_2024[1],
        metric="revenue",
    )

    summary = build_summary(decision, result)

    assert "Наибольший рост: «Дельта»" not in summary
    assert "Новый участник: «Дельта»" in summary
    assert "наибольшее падение: «Альфа»" in summary


def test_group_dynamics_payload_has_no_duplicate() -> None:
    """Одна группа: payload несёт одну запись, а не дубль best/worst (028)."""
    result = pd.DataFrame(
        {
            "product_group": ["Электроника"],
            "period1_value": [100.0],
            "period2_value": [285000.0],
            "delta": [185000.0],
            "delta_pct": [1850.0],
            "direction": ["up"],
        }
    )
    decision = _decision(
        "group_dynamics",
        period1=DATE_PAIR_2023_2024[0],
        period2=DATE_PAIR_2023_2024[1],
        metric="revenue",
    )

    payload = build_summary_payload(decision, result)

    assert len(payload.values) == 1


def test_lost_counterparty_is_not_ranked_as_decline() -> None:
    """«Выпавший» не называется «наибольшим падением» (симметрично zero-base)."""
    result = pd.DataFrame(
        {
            "counterparty": ["Альфа", "Бета"],
            "period1_value": [165000.0, 50000.0],
            "period2_value": [0.0, 40000.0],
            "delta": [-165000.0, -10000.0],
            "delta_pct": [-100.0, -20.0],
            "direction": ["lost", "down"],
        }
    )
    decision = _decision(
        "counterparty_dynamics",
        period1=DATE_PAIR_2023_2024[0],
        period2=DATE_PAIR_2023_2024[1],
        metric="revenue",
    )

    summary = build_summary(decision, result)

    assert "«Альфа» отсутствует в периоде 2 (в периоде 1 — 165 000,00 ₽)" in summary
    assert "наибольшее падение: «Альфа»" not in summary
    assert "наибольшее падение: «Бета»" in summary
