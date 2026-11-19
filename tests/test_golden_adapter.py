"""Адаптер «эталон (ТЗ 6.3) → канон 6.4»: маппинг Приложения A (DECISIONS.md#6)."""

from __future__ import annotations

import pytest

from tests.golden.adapter import NonMappable, adapt_expected_dsl


def _case(dsl):
    return {"id": "test_x", "expected_dsl": dsl}


def test_direction_maps_to_ascending() -> None:
    top = adapt_expected_dsl(_case({"intent": "top_bottom_products", "params": {"direction": "top"}}))
    bottom = adapt_expected_dsl(_case({"intent": "top_bottom_products", "params": {"direction": "bottom"}}))
    assert top["params"]["ascending"] is False
    assert bottom["params"]["ascending"] is True


def test_granularity_maps_to_freq() -> None:
    month = adapt_expected_dsl(_case({"intent": "timeseries", "params": {"granularity": "month"}}))
    quarter = adapt_expected_dsl(_case({"intent": "timeseries", "params": {"granularity": "quarter"}}))
    assert month["params"]["freq"] == "M"
    assert quarter["params"]["freq"] == "Q"


def test_dimension_maps_to_group_by() -> None:
    got = adapt_expected_dsl(
        _case({"intent": "negative_revenue", "params": {"dimension": "counterparty"}})
    )
    assert got["params"]["group_by"] == "counterparty"


def test_metrics_maps_to_metric() -> None:
    got = adapt_expected_dsl(_case({"intent": "timeseries", "params": {"metrics": ["quantity"]}}))
    assert got["params"]["metric"] == "quantity"


def test_period_string_maps_to_pair() -> None:
    got = adapt_expected_dsl(
        _case(
            {
                "intent": "group_dynamics",
                "params": {
                    "period1": "2022-01-01_2022-12-31",
                    "period2": "2023-01-01_2023-12-31",
                },
            }
        )
    )
    assert got["params"]["period1"] == ["2022-01-01", "2022-12-31"]
    assert got["params"]["period2"] == ["2023-01-01", "2023-12-31"]


def test_limit_maps_to_top_n_for_compare() -> None:
    got = adapt_expected_dsl(
        _case({"intent": "compare_top_products", "params": {"limit": 5}})
    )
    assert got["params"]["top_n"] == 5
    assert "limit" not in got["params"]


def test_products_are_non_mappable() -> None:
    with pytest.raises(NonMappable, match="products"):
        adapt_expected_dsl(
            _case({"intent": "compare_top_products", "params": {"products": ["Колонка Му"]}})
        )


def test_revenue_vat_is_non_mappable() -> None:
    with pytest.raises(NonMappable, match="revenue_vat"):
        adapt_expected_dsl(_case({"intent": "total", "params": {"metrics": ["revenue_vat"]}}))


def test_unknown_case_returns_none() -> None:
    assert adapt_expected_dsl({"id": "test_x", "expected_dsl": None}) is None
