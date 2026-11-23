"""Правило знака 007 и нормализация периодов (DECISIONS #31, #32).

007 действует только на записи ``metric="delta"``: знак дельты должен быть
подтверждён текстом рядом с сущностью. ``negative_revenue`` с отрицательной
выручкой — явление возвратов, под правило не попадает.
"""

from __future__ import annotations

import pytest

from tests.golden.checks import check_delta_signs, normalize_period, period_variants


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2023", "2023"),
        ("2023-11", "2023-11"),
        ("ноябрь 2023", "2023-11"),
        ("11.2023", "2023-11"),
        ("2024-Q1", "2024-Q1"),
        ("2023-11-01", "2023-11-01"),
        ("2023-11-01–2023-12-31", "2023-11-01–2023-12-31"),
        ("период с 01.11.2023 по 31.12.2023", "2023-11-01–2023-12-31"),
        ("01.11.2023–31.12.2023", "2023-11-01–2023-12-31"),
    ],
)
def test_normalize_period(raw: str, expected: str) -> None:
    assert normalize_period(raw) == expected


def test_period_variants_include_range_forms() -> None:
    variants = period_variants("2023-11-01–2023-12-31")

    assert "01.11.2023" in variants
    assert "31.12.2023" in variants
    assert "с 01.11.2023 по 31.12.2023" in variants


def _payload(*rows: dict) -> dict:
    return {"values": list(rows)}


def test_delta_negative_requires_fall_marker() -> None:
    payload = _payload(
        {"item": "ООО Лютик", "metric": "delta", "period": "2024", "value": -3_994_953.52},
        {"item": "Бытовая техника", "metric": "delta", "period": "2024", "value": -148_000.0},
    )
    check_delta_signs(
        "Наибольшее падение: «ООО Лютик» (-<сумма> руб.); "
        "наименьшее падение: «Бытовая техника» (-<сумма> руб.).",
        payload,
    )

    with pytest.raises(AssertionError, match="маркера падения"):
        check_delta_signs(
            "«ООО Лютик» изменился (-<сумма> руб.).", payload
        )


def test_delta_positive_requires_growth_marker() -> None:
    payload = _payload(
        {"item": "Ассоль", "metric": "delta", "period": "2024", "value": 12_000.0}
    )

    check_delta_signs("Наибольший рост: «Ассоль» (+12 000,00 руб.).", payload)

    with pytest.raises(AssertionError, match="маркера роста"):
        check_delta_signs("«Ассоль» (+12 000,00 руб.).", payload)


def test_delta_without_entity_in_text_fails() -> None:
    payload = _payload(
        {"item": "Ассоль", "metric": "delta", "period": "2024", "value": -100.0}
    )

    with pytest.raises(AssertionError, match="не названа"):
        check_delta_signs("Наибольшее падение: «Другая компания» (-100,00 руб.).", payload)


def test_delta_zero_is_not_checked() -> None:
    payload = _payload(
        {"item": "Ассоль", "metric": "delta", "period": "2024", "value": 0.0}
    )

    check_delta_signs("«Ассоль» без изменений.", payload)


def test_negative_revenue_is_out_of_007_scope() -> None:
    payload = _payload(
        {"item": "Возвраты", "metric": "revenue", "period": "2024", "value": -29_583.33}
    )

    check_delta_signs("Возвраты за 2024: -<сумма> руб.", payload)
