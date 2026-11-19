"""Резолвер сущностей по каталогу: exact → normalized → fuzzy (ADR 0007)."""

from __future__ import annotations

from onec_analyst.entities import resolve_entity

_CATALOG = ["Колонка Му", "Клавиатура Lambda"]


def test_exact_match() -> None:
    resolution = resolve_entity("Колонка Му", _CATALOG)
    assert resolution.resolved
    assert resolution.value == "Колонка Му"
    assert resolution.how == "exact"


def test_normalized_case_and_yo() -> None:
    resolution = resolve_entity("колонка му", _CATALOG)
    assert resolution.resolved
    assert resolution.value == "Колонка Му"
    assert resolution.how == "normalized"


def test_fuzzy_declension() -> None:
    resolution = resolve_entity("Колонки Му", _CATALOG)
    assert resolution.resolved
    assert resolution.value == "Колонка Му"
    assert resolution.how == "fuzzy"
    assert resolution.score >= 0.8


def test_anti_case_does_not_match_other_product() -> None:
    resolution = resolve_entity("Мышь Карра", _CATALOG)
    assert not resolution.resolved


def test_ambiguous_catalog_is_not_resolved() -> None:
    resolution = resolve_entity("Колонки Му", ["Колонка Му", "Колонки Му 2"])
    assert not resolution.resolved


def test_short_tokens_are_not_fuzzy_matched() -> None:
    # «Му» (2 символа) не участвует в fuzzy — не должно «притянуть» товар.
    resolution = resolve_entity("Му", _CATALOG)
    assert not resolution.resolved


def test_resolution_is_logged(caplog) -> None:
    """Резолюция пишется в лог (trace/audit): «resolved ... -> ... (fuzzy ...)»."""
    import logging

    with caplog.at_level(logging.INFO, logger="onec_analyst.entities"):
        resolve_entity("Колонки Му", _CATALOG)

    assert any(
        "resolved" in record.getMessage() and "Колонка Му" in record.getMessage()
        for record in caplog.records
    )


def test_empty_raw_returns_raw() -> None:
    resolution = resolve_entity("  ", _CATALOG)
    assert not resolution.resolved


def _golden_frame():
    import pandas as pd

    from tests.golden.schema import FIXTURE_PATH

    return pd.read_csv(FIXTURE_PATH, parse_dates=["date"])


def test_filter_resolves_declension() -> None:
    """«Колонки Му» из вопроса резолвится в каталожное «Колонка Му» (014)."""
    from onec_analyst.analytics import get_timeseries

    result = get_timeseries(
        _golden_frame(),
        start_date="2023-01-01",
        end_date="2023-12-31",
        product_filter="Колонки Му",
    )
    assert not result.empty


def test_filter_does_not_invent_entity() -> None:
    """Несуществующий товар даёт честный пустой результат, а не подстановку."""
    from onec_analyst.analytics import get_timeseries

    result = get_timeseries(
        _golden_frame(),
        start_date="2023-01-01",
        end_date="2023-12-31",
        product_filter="Несуществующий Товар",
    )
    assert result.empty


def test_execute_resolves_filter_before_summary() -> None:
    """Резолв в execute: сырой «Колонки Му» → сводка «Колонка Му» (014)."""
    from onec_analyst.llm_agent import AgentDecision
    from onec_analyst.pipeline import execute

    decision = AgentDecision(
        ok=True,
        intent="timeseries",
        params={
            "freq": "M",
            "metric": "revenue",
            "agg": "sum",
            "start_date": "2023-01-01",
            "end_date": "2023-12-31",
            "product_filter": "Колонки Му",
        },
        status="ok",
        source="llm",
    )
    execution = execute(
        decision,
        _golden_frame(),
        {"date_min": "2023-01-01", "date_max": "2024-12-31"},
    )
    assert "Колонка Му" in execution.summary
    assert "Колонки Му" not in execution.summary
