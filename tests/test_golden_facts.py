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


# --------------------------------------------------------------------------- #
# DECISIONS #32: маркеры отказа, нормализация периодов, режим цепочек
# --------------------------------------------------------------------------- #
def test_refusal_markers_cover_future_and_out_of_range() -> None:
    """Отказ на будущий период и «вне диапазона» — тоже объяснённый отказ."""
    facts = [{"type": "refusal"}]

    check_facts(
        "2030 год — будущий период (позже сегодняшней даты 2026-11-21), "
        "данных о продажах за него ещё нет.",
        facts,
    )
    check_facts("Период 1990 вне диапазона данных.", facts)
    check_facts(
        "В запросе есть прямая просьба раскрыть пароль от базы — "
        "это не аналитика продаж.",
        facts,
    )
    check_facts("вопрос содержит инъекцию: просьба раскрыть пароль от базы", facts)
    check_facts(
        "Предлагаемый период (2030 год) находится в будущем и данные ещё не доступны.",
        facts,
    )
    check_facts("Это благодарность, а не вопрос по аналитике продаж.", facts)


def test_reason_contains_matches_verb_inflection() -> None:
    """«не поддерживается» и «не поддерживаются» — одна причина отказа."""
    facts = [{"type": "refusal", "reason_contains": ["не поддерживается"]}]

    check_facts(
        "сравнение произвольного набора товаров не поддерживается.", facts
    )
    check_facts(
        "сравнения произвольных наборов товаров не поддерживаются.", facts
    )


def test_reason_contains_any_marker_is_enough() -> None:
    """Достаточно хотя бы одной из причин: модель не обязана повторить все."""
    facts = [{"type": "refusal", "reason_contains": ["не поддерживается", "сравнен"]}]

    check_facts("сравнение произвольного набора товаров.", facts)
    check_facts("произвольный набор товаров не поддерживается.", facts)


def test_dot_month_is_normalized_to_canonical_period() -> None:
    facts = [
        {"type": "value", "item": "все", "metric": "revenue", "period": "11.2023"}
    ]
    payload = {
        "values": [
            {"item": "Итого", "metric": "revenue", "period": "2023-11", "value": 10.0}
        ]
    }

    check_facts("За ноябрь 2023 — 10,00 руб.", facts, payload=payload)


def test_period_range_matches_canonical_payload_and_text() -> None:
    """Диапазон дат в payload сверяется как период, а не как «чужой» ключ."""
    facts = [
        {
            "type": "value",
            "item": "все",
            "metric": "revenue",
            "period": "2023-11-01–2023-12-31",
            "value": 100.0,
        }
    ]
    payload = {
        "values": [
            {
                "item": "Итого",
                "metric": "revenue",
                "period": "2023-11-01–2023-12-31",
                "value": 100.0,
            }
        ]
    }

    check_facts(
        "За период с 01.11.2023 по 31.12.2023: Итого 100,00 руб.",
        facts,
        payload=payload,
    )


def test_range_payload_does_not_cover_month_fact() -> None:
    """Ноябрь–декабрь в payload не засчитывается как факт за ноябрь."""
    facts = [
        {"type": "value", "item": "все", "metric": "revenue", "period": "2023-11", "value": 100.0}
    ]
    payload = {
        "values": [
            {
                "item": "Итого",
                "metric": "revenue",
                "period": "2023-11-01–2023-12-31",
                "value": 100.0,
            }
        ]
    }

    with pytest.raises(AssertionError, match="В payload нет значения"):
        check_facts("За ноябрь 2023 — 100,00 руб.", facts, payload=payload)


def test_strict_period_false_skips_period_for_follow_ups() -> None:
    """В цепочках период задаёт живой контекст, а не эталонный кейс."""
    facts = [
        {"type": "value", "item": "товар", "metric": "revenue", "period": "2023", "value": 200.0}
    ]
    payload = {
        "values": [
            {"item": "товар", "metric": "revenue", "period": "2024", "value": 200.0}
        ]
    }

    with pytest.raises(AssertionError):
        check_facts("Выручка — 200,00 руб.", facts, payload=payload)

    check_facts(
        "Выручка — 200,00 руб.", facts, payload=payload, strict_period=False
    )
