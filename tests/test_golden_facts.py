"""Тесты факт-чека: цикл не прерывается после первого факта."""

from __future__ import annotations

import pytest

from tests.golden.checks import check_facts


def test_check_facts_checks_every_fact() -> None:
    facts = [
        {"type": "value", "item": "товар", "metric": "revenue", "period": "2023"},
        {"type": "value", "item": "товар", "metric": "revenue", "period": "2024"},
    ]

    with pytest.raises(AssertionError, match="Факт 2"):
        check_facts("Выручка за 2023 год составила 100 руб.", facts)


def test_check_facts_accepts_all_facts() -> None:
    facts = [
        {"type": "value", "item": "товар", "metric": "revenue", "period": "2023"},
        {"type": "value", "item": "товар", "metric": "revenue", "period": "2024"},
    ]

    check_facts(
        "Выручка за 2023 год — 100 руб., за 2024 год — 200 руб.",
        facts,
    )


def test_refusal_reason_must_be_explained() -> None:
    facts = [
        {
            "type": "refusal",
            "reason_contains": ["не поддерживается", "сравнен"],
        }
    ]

    check_facts("Сравнение произвольного набора товаров не поддерживается.", facts)

    with pytest.raises(AssertionError, match="Факт 1"):
        check_facts("Извините, не могу.", facts)


def test_clarify_checks_missing_from_plan() -> None:
    facts = [{"type": "clarify", "missing": "products"}]

    check_facts(
        "Уточните, пожалуйста, какие товары.",
        facts,
        clarification_missing=["product_filter"],
    )

    with pytest.raises(AssertionError, match="Факт 1"):
        check_facts(
            "Уточните, пожалуйста, какие товары.",
            facts,
            clarification_missing=["period2"],
        )


def test_clarify_falls_back_to_text() -> None:
    check_facts(
        "Уточните, какие товары вы имеете в виду?",
        [{"type": "clarify", "missing": "products"}],
    )


def test_numeric_fact_requires_payload() -> None:
    facts = [
        {
            "type": "value",
            "item": "товар",
            "metric": "revenue",
            "period": "2023",
            "value": 123.45,
        }
    ]

    with pytest.raises(AssertionError, match="SummaryPayload"):
        check_facts("Выручка за 2023 год — 123,45 руб.", facts)


def test_numeric_fact_is_checked_against_payload() -> None:
    facts = [
        {
            "type": "value",
            "item": "товар",
            "metric": "revenue",
            "period": "2023",
            "value": 123.45,
        }
    ]
    payload = {
        "values": [
            {"item": "товар", "metric": "revenue", "period": "2023", "value": 123.45}
        ]
    }

    check_facts("Выручка за 2023 год — 123,45 руб.", facts, payload=payload)

    payload["values"][0]["value"] = 124.0
    with pytest.raises(AssertionError, match="Число в payload"):
        check_facts("Выручка за 2023 год — 123,45 руб.", facts, payload=payload)


def test_generic_item_matches_aggregate_payload() -> None:
    """Заглушка «все» находит агрегат «Итого» по метрике и периоду."""
    facts = [
        {
            "type": "value",
            "item": "все",
            "metric": "revenue",
            "period": "2023",
            "value": 100.0,
        }
    ]
    payload = {
        "values": [
            {"item": "Итого", "metric": "revenue", "period": "2023", "value": 100.0}
        ]
    }

    check_facts("Выручка за 2023 год — 100,00 руб.", facts, payload=payload)


def test_unknown_fact_type_is_rejected() -> None:
    with pytest.raises(AssertionError, match="неизвестный тип"):
        check_facts("что-то", [{"type": "forecast"}])
