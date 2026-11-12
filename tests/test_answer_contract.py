"""Тесты контракта ответа MCP-фасада (BS-01, условие 1).

Контракт фиксирует: период и дата данных есть в каждом ответе, период не
наследуется из истории, отсутствие выгрузки не замалчивается, отказ перечисляет
доступные срезы, усечение таблицы помечается.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from onec_analyst.answer_contract import (
    AnswerMeta,
    PeriodResolution,
    format_answer,
    format_period,
    format_refusal,
    resolve_period,
)

AS_OF = datetime(2026, 11, 12, 7, 5)
DATA_MIN = date(2023, 1, 1)
DATA_MAX = date(2024, 12, 28)
FULL_PERIOD = PeriodResolution(date(2024, 1, 1), date(2024, 12, 31), True)


def test_answer_contains_period_data_intent_and_rows() -> None:
    meta = AnswerMeta(
        intent="top_bottom_products",
        params={"metric": "revenue", "limit": 5},
        period=FULL_PERIOD,
        as_of=AS_OF,
        shown=5,
        total=5,
    )

    text = format_answer("| rank | product |\n|---|---|\n| 1 | Beta |", meta)

    assert "Период: 01.01.2024–31.12.2024" in text
    assert "Данные: выгрузка 12.11.2026 07:05" in text
    assert "Срез: top_bottom_products (limit=5, metric=revenue)" in text
    assert "Показаны все 5." in text
    assert "| rank | product |" in text


def test_unspecified_period_is_taken_from_data_and_marked() -> None:
    period = resolve_period(None, None, data_min=DATA_MIN, data_max=DATA_MAX)

    assert period == PeriodResolution(DATA_MIN, DATA_MAX, False)

    text = format_answer("x", AnswerMeta(intent="total", period=period, as_of=AS_OF))

    assert "Период: 01.01.2023–28.12.2024" in text
    assert "Период не указан — взят весь доступный период витрины." in text


def test_resolve_period_fills_one_missing_boundary() -> None:
    period = resolve_period(date(2024, 6, 1), None, data_min=DATA_MIN, data_max=DATA_MAX)

    assert period.start == date(2024, 6, 1)
    assert period.end == DATA_MAX
    assert period.specified is True


def test_resolve_period_rejects_reversed_range() -> None:
    with pytest.raises(ValueError):
        resolve_period(
            date(2024, 12, 31),
            date(2024, 1, 1),
            data_min=DATA_MIN,
            data_max=DATA_MAX,
        )


def test_data_line_falls_back_to_file_mtime() -> None:
    meta = AnswerMeta(
        intent="total",
        period=PeriodResolution(),
        file_mtime=datetime(2026, 11, 11, 19, 0),
    )

    assert "Данные: время файла 11.11.2026 19:00" in format_answer("x", meta)


def test_data_line_is_never_silently_missing() -> None:
    text = format_answer("x", AnswerMeta(intent="total", period=PeriodResolution()))

    assert "Период: не определён" in text
    assert "Данные: дата выгрузки неизвестна" in text


def test_refusal_lists_available_intents_without_dataset() -> None:
    text = format_refusal(
        "«топ товаров по контрагенту»",
        ["total", "negative_revenue", "group_dynamics"],
    )

    assert text.startswith("Не поддерживается: «топ товаров по контрагенту».")
    assert "Данные: витрина не загружена" in text
    assert "Доступно: total, negative_revenue, group_dynamics." in text


def test_refusal_keeps_period_and_data_when_dataset_loaded() -> None:
    meta = AnswerMeta(
        intent="refusal",
        period=PeriodResolution(DATA_MIN, DATA_MAX, True),
        as_of=AS_OF,
    )

    text = format_refusal("маржу", ["total"], meta=meta)

    assert "Период: 01.01.2023–28.12.2024" in text
    assert "Данные: выгрузка 12.11.2026 07:05" in text
    assert "Срез:" not in text


def test_params_with_period_lists_are_readable() -> None:
    meta = AnswerMeta(
        intent="group_dynamics",
        params={
            "period1": [date(2023, 1, 1), date(2023, 12, 31)],
            "period2": [date(2024, 1, 1), date(2024, 12, 31)],
            "only_drops": True,
            "group_by": "product_group",
        },
        period=PeriodResolution(DATA_MIN, DATA_MAX, True),
        as_of=AS_OF,
    )

    text = format_answer("x", meta)

    assert "period1=2023-01-01…2023-12-31" in text
    assert "period2=2024-01-01…2024-12-31" in text
    assert "only_drops=true" in text


def test_truncation_marked_when_rows_cut() -> None:
    meta = AnswerMeta(
        intent="top_bottom_products",
        period=PeriodResolution(DATA_MIN, DATA_MAX, True),
        as_of=AS_OF,
        shown=50,
        total=120,
    )

    assert "Показаны первые 50 из 120." in format_answer("x", meta)


def test_format_period_partial_bounds() -> None:
    assert format_period(None, date(2024, 12, 31)) == "…–31.12.2024"
    assert format_period(date(2024, 1, 1), None) == "01.01.2024–…"
    assert format_period(None, None) == "не определён"
