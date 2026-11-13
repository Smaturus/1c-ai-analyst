"""Тесты MCP-сервера: инструменты, контракт ответа, отказы и безопасность путей."""

from __future__ import annotations

import asyncio
from datetime import date

from onec_analyst import mcp_server
from onec_analyst.mcp_server import (
    describe_capabilities,
    describe_dataset,
    group_dynamics,
    list_dimensions,
    load_dataset,
    mcp,
    negative_revenue,
    timeseries,
    total,
    top_bottom_products,
)

EXPECTED_TOOLS = {
    "load_dataset",
    "describe_dataset",
    "describe_capabilities",
    "total",
    "top_bottom_products",
    "compare_top_products",
    "negative_revenue",
    "timeseries",
    "counterparty_dynamics",
    "group_dynamics",
    "returns_alert",
    "list_dimensions",
}


def _tool_names() -> set[str]:
    return {tool.name for tool in asyncio.run(mcp.list_tools())}


def _call(name: str, arguments: dict[str, object]) -> str:
    content, _ = asyncio.run(mcp.call_tool(name, arguments))
    return "\n".join(block.text for block in content if block.type == "text")


def test_all_slices_registered_and_no_generic_dsl() -> None:
    names = _tool_names()

    assert EXPECTED_TOOLS <= names
    assert "execute_dsl" not in names
    assert "ask_analyst" not in names


def test_total_accepts_string_dates_over_protocol() -> None:
    text = _call("total", {"start_date": "2024-01-01", "end_date": "2024-12-31"})

    assert "Период: 01.01.2024–31.12.2024" in text
    assert "Данные:" in text
    assert "52 253 500,00" in text
    assert "start_date=" not in text  # параметры скрыты по умолчанию


def test_total_without_period_is_explicitly_marked() -> None:
    text = total()

    assert "Период: 01.01.2023–28.12.2024" in text
    assert "Период не указан — взят весь доступный период витрины." in text


def test_top_bottom_returns_engine_numbers() -> None:
    text = top_bottom_products(
        limit=5,
        start_date=date(2024, 1, 1),
        end_date=date(2024, 12, 31),
    )

    assert "Ноутбук Beta" in text
    assert "15 820 000,00" in text
    assert "Показаны все 5." in text


def test_top_bottom_summary_is_computed_by_server() -> None:
    text = top_bottom_products(
        limit=5,
        start_date=date(2024, 1, 1),
        end_date=date(2024, 12, 31),
    )

    assert "Сумма по показанным строкам: 48 105 000,00 ₽" in text
    assert "доля в выручке за период: 92,06 %" in text


def test_top_bottom_summary_is_skipped_for_group_filter() -> None:
    text = top_bottom_products(
        limit=5,
        group_filter="Аксессуары",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 12, 31),
    )

    assert "Сумма по показанным строкам" not in text
    assert "Фильтр: группа «Аксессуары»." in text


def test_list_dimensions_lists_groups() -> None:
    text = list_dimensions(kind="groups")

    assert "Аксессуары" in text
    assert "Электроника" in text


def test_list_dimensions_lists_counterparties() -> None:
    text = list_dimensions(kind="counterparties", limit=10)

    assert "ООО Лютик" in text


def test_pair_periods_are_reported() -> None:
    text = group_dynamics(
        period1_start=date(2023, 1, 1),
        period1_end=date(2023, 12, 31),
        period2_start=date(2024, 1, 1),
        period2_end=date(2024, 12, 31),
        only_drops=True,
    )

    assert "Сравниваются периоды: 01.01.2023–31.12.2023 и 01.01.2024–31.12.2024." in text
    assert "Аксессуары" in text
    assert "Период: 01.01.2023–31.12.2024" in text


def test_negative_revenue_by_counterparty() -> None:
    text = negative_revenue(
        start_date=date(2024, 1, 1),
        end_date=date(2024, 12, 31),
        group_by="counterparty",
    )

    assert "Контрагент" in text
    assert "ООО Лютик" in text


def test_table_truncation_is_marked() -> None:
    text = timeseries(freq="D")

    assert "Показаны первые 50 из" in text


def test_load_dataset_rejects_paths_outside_repo() -> None:
    text = load_dataset(path="C:/Windows/win.ini")

    assert "вне каталога проекта" in text


def test_describe_dataset_reports_loaded_vitrina() -> None:
    load_dataset()
    text = describe_dataset()

    assert "Витрина: sample_data.csv" in text
    assert "Строк: 600" in text


def test_capabilities_explain_unsupported_questions() -> None:
    text = describe_capabilities()

    assert "Поддерживаемые срезы:" in text
    assert "Не поддерживается: маржа и себестоимость" in text


def test_verbose_mode_shows_slice_parameters(monkeypatch) -> None:
    monkeypatch.setenv(mcp_server.VERBOSE_ENV, "1")

    text = total(start_date=date(2024, 1, 1), end_date=date(2024, 12, 31))

    assert "start_date=2024-01-01" in text
