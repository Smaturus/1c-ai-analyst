"""Unit-тесты отображения: русские заголовки, форматы, сортировка и графики.

Проверяют доработку UX по замечанию эксперта: в таблице нет технических имён
полей (``period``, ``revenue``, ``delta``), значения выводятся в бизнес-формате,
но остаются числами/датами — поэтому столбцы сортируются по клику на заголовок.
Отдельный блок — спецификация Altair-графиков (оси, сортировка, цвет) без рендера.
"""

from __future__ import annotations

import math

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from app import (
    build_chart,
    build_column_config,
    build_result_table,
    build_summary,
    format_executed_filter,
    format_metric,
    format_percent,
    format_quantity,
    _period_column_format,
)
from onec_analyst.llm_agent import AgentDecision
from tests.paths import APP_FILE


def _decision(intent: str, **params: object) -> AgentDecision:
    """Собрать решение планировщика для проверки отрисовки."""
    return AgentDecision(ok=True, intent=intent, params=dict(params), source="rules")


# --------------------------------------------------------------------------- #
# Безопасное форматирование значений в текстовых сводках
# --------------------------------------------------------------------------- #
def test_format_quantity_drops_zero_fraction() -> None:
    """Целое количество печатается без «,00», большое — с разрядами."""
    assert format_quantity(595) == "595"
    assert format_quantity(595.0) == "595"
    assert format_quantity(10_878) == "10 878"
    assert format_quantity(1.5) == "1,5"


@pytest.mark.parametrize("value", [None, math.nan, math.inf, "abc", object()])
def test_format_quantity_is_safe_on_garbage(value: object) -> None:
    """Мусорные значения дают прочерк, а не падение страницы."""
    assert format_quantity(value) == "—"


def test_format_metric_uses_russian_number_style() -> None:
    """Выручка — с разрядами и запятой, количество — без хвостовых нулей."""
    assert format_metric(1_234.5, "revenue") == "1 234,50"
    assert format_metric(1234, "quantity") == "1 234"


def test_format_percent_uses_comma_and_is_safe() -> None:
    """Проценты печатаются с запятой, мусор — прочерком."""
    assert format_percent(66.666) == "66,67"
    assert format_percent(-100) == "-100,00"
    assert format_percent(None) == "—"


# --------------------------------------------------------------------------- #
# Сортируемость: данные остаются числами и датами
# --------------------------------------------------------------------------- #
def test_total_table_keeps_numbers_and_dates_sortable() -> None:
    """Таблица итога передаёт в st.dataframe числа и даты, а не строки."""
    result = pd.DataFrame(
        [
            {
                "period_start": pd.Timestamp("2024-01-01"),
                "period_end": pd.Timestamp("2024-12-31"),
                "revenue": 1_717_876.1,
                "quantity": 595.0,
                "rows_count": 284,
                "undated_rows": 0,
            }
        ]
    )

    display = build_result_table(_decision("total"), result)

    assert pd.api.types.is_float_dtype(display["revenue"])
    assert pd.api.types.is_float_dtype(display["quantity"])
    assert pd.api.types.is_integer_dtype(display["rows_count"])
    assert pd.api.types.is_datetime64_any_dtype(display["period_start"])
    assert display.iloc[0]["quantity"] == 595.0


def test_dynamics_table_translates_direction_but_keeps_text_sortable() -> None:
    """Направление выводится словами, колонка остаётся текстовой (сортируется)."""
    result = pd.DataFrame(
        [
            {
                "counterparty": "B",
                "period1_value": 200.0,
                "period2_value": 40.0,
                "delta": -160.0,
                "delta_pct": -80.0,
                "direction": "down",
            }
        ]
    )

    display = build_result_table(_decision("counterparty_dynamics"), result)

    assert display.iloc[0]["direction"] == "падение"
    assert pd.api.types.is_string_dtype(display["direction"])
    assert pd.api.types.is_float_dtype(display["delta"])


def test_percent_columns_are_rounded_but_stay_numeric() -> None:
    """Проценты округляются до двух знаков, оставаясь числами."""
    result = pd.DataFrame([{"product": "P1", "revenue": 120.0, "share_pct": 66.666_666}])

    display = build_result_table(_decision("top_bottom_products"), result)

    assert pd.api.types.is_float_dtype(display["share_pct"])
    assert display.iloc[0]["share_pct"] == pytest.approx(66.67)


# --------------------------------------------------------------------------- #
# Русские заголовки и форматы колонок
# --------------------------------------------------------------------------- #
def test_total_column_config_labels_and_formats() -> None:
    """Итог: русские подписи, локализованные числа с двумя знаками, даты ДД.ММ.ГГГГ."""
    result = pd.DataFrame(
        [
            {
                "period_start": pd.Timestamp("2024-01-01"),
                "period_end": pd.Timestamp("2024-12-31"),
                "revenue": 1_717_876.1,
                "quantity": 595.0,
                "rows_count": 284,
                "undated_rows": 0,
            }
        ]
    )

    config = build_column_config(_decision("total"), result)

    assert config["period_start"]["label"] == "Начало периода"
    assert config["period_start"]["type_config"]["format"] == "DD.MM.YYYY"
    assert config["revenue"]["label"] == "Выручка без НДС, ₽"
    assert config["revenue"]["type_config"]["format"] == "localized"
    assert config["revenue"]["type_config"]["step"] == 0.01
    assert config["quantity"]["label"] == "Количество, шт."
    assert config["rows_count"]["label"] == "Строк в выборке"
    assert config["undated_rows"]["label"] == "Строк без даты"


def test_top_column_config_labels() -> None:
    """Топ: «№», «Товар», «Доля, %» вместо rank/product/share_pct."""
    result = pd.DataFrame(
        [{"rank": 1, "product": "P1", "revenue": 120.0, "share_pct": 66.67}]
    )

    config = build_column_config(
        _decision("top_bottom_products", metric="revenue"), result
    )

    assert [config[column]["label"] for column in result.columns] == [
        "№",
        "Товар",
        "Выручка без НДС, ₽",
        "Доля, %",
    ]
    assert config["share_pct"]["type_config"]["step"] == 0.01


def test_top_column_config_uses_quantity_units() -> None:
    """Для метрики quantity заголовок — «Количество, шт.», без фиксации двух знаков."""
    result = pd.DataFrame(
        [{"rank": 1, "product": "P1", "quantity": 30.0, "share_pct": 50.0}]
    )

    config = build_column_config(
        _decision("top_bottom_products", metric="quantity"), result
    )

    assert config["quantity"]["label"] == "Количество, шт."
    assert "step" not in config["quantity"]["type_config"] or config["quantity"][
        "type_config"
    ]["step"] is None


@pytest.mark.parametrize(
    ("freq", "expected"),
    [("M", "MM.YYYY"), ("Q", "Q YYYY"), ("Y", "YYYY"), ("D", "DD.MM.YYYY")],
)
def test_timeseries_period_format_depends_on_frequency(freq: str, expected: str) -> None:
    """Месяц — «MM.YYYY», квартал — «Q YYYY», год — «YYYY», день — дата."""
    result = pd.DataFrame([{"period": pd.Timestamp("2024-01-01"), "revenue": 100.0}])

    config = build_column_config(_decision("timeseries", metric="revenue", freq=freq), result)

    assert _period_column_format(freq) == expected
    assert config["period"]["type_config"]["format"] == expected
    assert config["period"]["label"] == "Период"


def test_dynamics_column_config_shows_period_dates() -> None:
    """Колонки периодов подписаны датами, направление — текстовая колонка."""
    result = pd.DataFrame(
        [
            {
                "counterparty": "B",
                "period1_value": 200.0,
                "period2_value": 40.0,
                "delta": -160.0,
                "delta_pct": -80.0,
                "direction": "down",
            }
        ]
    )
    decision = _decision(
        "counterparty_dynamics",
        metric="revenue",
        period1=("2023-01-01", "2023-12-31"),
        period2=("2024-01-01", "2024-12-31"),
        only_drops=True,
    )

    config = build_column_config(decision, result)

    assert config["counterparty"]["label"] == "Контрагент"
    assert config["period1_value"]["label"] == "01.01.2023–31.12.2023, ₽"
    assert config["period2_value"]["label"] == "01.01.2024–31.12.2024, ₽"
    assert config["delta"]["label"] == "Изменение, ₽"
    assert config["delta_pct"]["label"] == "Изменение, %"
    assert config["direction"]["label"] == "Направление"
    assert config["direction"]["type_config"]["type"] == "text"


# --------------------------------------------------------------------------- #
# Пустой результат и текстовая сводка
# --------------------------------------------------------------------------- #
def test_empty_result_produces_empty_table_and_config() -> None:
    """Пустой результат не ломает отрисовку."""
    empty = pd.DataFrame(columns=["period", "revenue"])

    assert build_result_table(_decision("timeseries"), empty).empty
    assert build_column_config(_decision("timeseries"), empty) == {}


def test_summary_uses_russian_number_style() -> None:
    """Текстовая сводка использует те же правила форматирования."""
    result = pd.DataFrame(
        [
            {
                "period_start": pd.Timestamp("2024-01-01"),
                "period_end": pd.Timestamp("2024-12-31"),
                "revenue": 1_717_876.1,
                "quantity": 595.0,
                "rows_count": 284,
                "undated_rows": 0,
            }
        ]
    )

    summary = build_summary(_decision("total"), result)

    assert "1 717 876,10 ₽" in summary
    assert "595 шт." in summary


# --------------------------------------------------------------------------- #
# Графики: Altair, русские оси, горизонтальные бары
# --------------------------------------------------------------------------- #
def _chart_spec(decision: AgentDecision, result: pd.DataFrame) -> dict:
    """Сериализовать график в Vega-Lite spec — проверки без рендера."""
    chart = build_chart(decision, result)
    assert chart is not None, "для этого интента график должен строиться"
    return chart.to_dict()


def _top_result() -> pd.DataFrame:
    """Результат топ/антитопа с длинными названиями товаров."""
    return pd.DataFrame(
        {
            "rank": [1, 2],
            "product": ["Вентилятор BINATONE ALPINE 160вт", "Мука цельнозерновая"],
            "quantity": [30.0, 20.0],
            "revenue": [120.0, 40.0],
            "share_pct": [75.0, 25.0],
        }
    )


def _dynamics_result() -> pd.DataFrame:
    """Результат динамики контрагентов (рост и падение)."""
    return pd.DataFrame(
        [
            {
                "counterparty": "A",
                "period1_value": 100.0,
                "period2_value": 120.0,
                "delta": 20.0,
                "delta_pct": 20.0,
                "direction": "up",
            },
            {
                "counterparty": "B",
                "period1_value": 200.0,
                "period2_value": 40.0,
                "delta": -160.0,
                "delta_pct": -80.0,
                "direction": "down",
            },
        ]
    )


def test_top_chart_is_horizontal_bar_with_quantity_axes() -> None:
    """Топ/антитоп: горизонтальный бар, оси «Товар» / «Количество, шт.»."""
    spec = _chart_spec(
        _decision("top_bottom_products", metric="quantity"), _top_result()
    )

    assert spec["mark"]["type"] == "bar"
    assert spec["encoding"]["x"]["field"] == "quantity"
    assert spec["encoding"]["x"]["type"] == "quantitative"
    assert spec["encoding"]["x"]["title"] == "Количество, шт."

    encoding_y = spec["encoding"]["y"]
    assert encoding_y["field"] == "product"
    assert encoding_y["type"] == "nominal"
    assert encoding_y["title"] == "Товар"
    assert encoding_y["sort"] == "-x"  # лидер сверху


def test_top_chart_revenue_axis_title() -> None:
    """Для метрики revenue ось X подписана «Выручка без НДС, ₽»."""
    spec = _chart_spec(
        _decision("top_bottom_products", metric="revenue"), _top_result()
    )

    assert spec["encoding"]["x"]["title"] == "Выручка без НДС, ₽"
    assert spec["encoding"]["x"]["type"] == "quantitative"


def test_dynamics_chart_uses_counterparty_and_delta_axes() -> None:
    """Динамика: x — «Изменение, ₽», y — «Контрагент» с сортировкой по значению."""
    spec = _chart_spec(
        _decision("counterparty_dynamics", metric="revenue"), _dynamics_result()
    )

    assert spec["mark"]["type"] == "bar"
    assert spec["encoding"]["x"]["field"] == "delta"
    assert spec["encoding"]["x"]["title"] == "Изменение, ₽"
    assert spec["encoding"]["y"]["field"] == "counterparty"
    assert spec["encoding"]["y"]["title"] == "Контрагент"
    assert spec["encoding"]["y"]["sort"] == "-x"


def test_dynamics_chart_colors_bars_by_direction() -> None:
    """Бары динамики красятся по направлению с русской легендой."""
    spec = _chart_spec(
        _decision("counterparty_dynamics", metric="revenue"), _dynamics_result()
    )

    color = spec["encoding"]["color"]
    assert color["field"] == "direction"
    assert color["title"] == "Направление"
    assert color["scale"]["domain"] == [
        "рост",
        "падение",
        "без изменений",
        "новый",
        "выпал",
    ]
    assert color["scale"]["range"][0] == "#2e7d32"
    assert color["scale"]["range"][1] == "#c62828"


def test_dynamics_chart_group_uses_group_axis_title() -> None:
    """Срез по группам: ось Y подписана «Товарная группа»."""
    result = pd.DataFrame(
        [
            {
                "product_group": "G1",
                "period1_value": 100.0,
                "period2_value": 80.0,
                "delta": -20.0,
                "delta_pct": -20.0,
                "direction": "down",
            }
        ]
    )

    spec = _chart_spec(_decision("group_dynamics", metric="revenue"), result)

    assert spec["encoding"]["y"]["field"] == "product_group"
    assert spec["encoding"]["y"]["title"] == "Товарная группа"


def test_timeseries_chart_is_line_with_russian_axes() -> None:
    """Временной ряд: линия, x — «Период», y — «Выручка без НДС, ₽»."""
    result = pd.DataFrame(
        {
            "period": pd.to_datetime(["2023-01-01", "2023-02-01"]),
            "revenue": [100.0, 200.0],
        }
    )

    spec = _chart_spec(_decision("timeseries", metric="revenue"), result)

    assert spec["mark"]["type"] == "line"
    assert spec["encoding"]["x"]["field"] == "period"
    assert spec["encoding"]["x"]["type"] == "temporal"
    assert spec["encoding"]["x"]["title"] == "Период"
    assert spec["encoding"]["y"]["field"] == "revenue"
    assert spec["encoding"]["y"]["title"] == "Выручка без НДС, ₽"


def test_timeseries_chart_quantity_axis_title() -> None:
    """Для метрики quantity ось Y — «Количество, шт.»."""
    result = pd.DataFrame(
        {"period": pd.to_datetime(["2023-01-01"]), "quantity": [5.0]}
    )

    spec = _chart_spec(_decision("timeseries", metric="quantity"), result)

    assert spec["encoding"]["y"]["title"] == "Количество, шт."


def test_timeseries_single_interval_is_bar() -> None:
    """Один интервал — столбец: линия из одной точки читается как «нет данных»."""
    result = pd.DataFrame(
        {"period": pd.to_datetime(["2023-10-01"]), "quantity": [435.0]}
    )

    spec = _chart_spec(_decision("timeseries", metric="quantity"), result)

    assert spec["mark"]["type"] == "bar"
    assert spec["encoding"]["x"]["title"] == "Период"
    assert spec["encoding"]["y"]["title"] == "Количество, шт."


def test_timeseries_two_intervals_stay_line() -> None:
    """Два интервала и больше — прежняя линия с точками."""
    result = pd.DataFrame(
        {
            "period": pd.to_datetime(["2023-01-01", "2023-02-01"]),
            "revenue": [100.0, 200.0],
        }
    )

    spec = _chart_spec(_decision("timeseries", metric="revenue"), result)

    assert spec["mark"]["type"] == "line"


def test_total_intent_has_no_altair_chart() -> None:
    """Итог рисуется metric-карточками, Altair-график не нужен."""
    result = pd.DataFrame(
        [
            {
                "period_start": pd.Timestamp("2024-01-01"),
                "period_end": pd.Timestamp("2024-12-31"),
                "revenue": 1.0,
                "quantity": 2.0,
                "rows_count": 3,
                "undated_rows": 0,
            }
        ]
    )

    assert build_chart(_decision("total"), result) is None


def test_chart_returns_none_for_empty_result() -> None:
    """Пустой результат — графика нет, таблица показывает «нет данных»."""
    assert (
        build_chart(_decision("top_bottom_products", metric="revenue"), pd.DataFrame())
        is None
    )


def test_chart_returns_none_for_missing_columns_or_unknown_intent() -> None:
    """Без нужных колонок или для неизвестного интента график не строится."""
    result = pd.DataFrame({"product": ["A"], "quantity": [1.0]})

    assert build_chart(_decision("top_bottom_products", metric="revenue"), result) is None
    assert build_chart(_decision("unknown_intent"), result) is None
    assert build_chart(_decision("timeseries"), result) is None


# --------------------------------------------------------------------------- #
# Бизнес-подсказка в сайдбаре: возвраты уменьшают выручку, нули не влияют
# --------------------------------------------------------------------------- #
@pytest.fixture()
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Офлайн-режим: тест не ходит в API и не зависит от .env."""
    monkeypatch.setenv("LLM_PROVIDER", "none")
    monkeypatch.delenv("LLM_API_KEY", raising=False)


def test_sidebar_explains_net_revenue_business_rule(offline: None) -> None:
    """В сайдбаре есть бизнес-подсказка про нетто-выручку и нулевые строки."""
    at = AppTest.from_file(APP_FILE, default_timeout=120).run()

    assert len(at.exception) == 0
    captions = [caption.value for caption in at.caption]
    assert any("Возвраты и сторно уменьшают итоговую выручку" in text for text in captions)
    assert any("Строки с нулевой выручкой не влияют на суммы" in text for text in captions)


# --------------------------------------------------------------------------- #
# Интент «Отрицательная выручка»: сводка и красный график
# --------------------------------------------------------------------------- #
def test_negative_revenue_summary() -> None:
    """Сводка: N товаров с отрицательной выручкой + общая сумма возвратов."""
    result = pd.DataFrame(
        {
            "product": ["A", "B"],
            "revenue": [-200.0, -50.0],
            "rows_count": [1, 2],
            "quantity": [-4.0, -2.0],
        }
    )

    summary = build_summary(_decision("negative_revenue", group_by="product"), result)

    assert "Найдено 2 товаров с отрицательной выручкой" in summary
    assert "Общая сумма возвратов: -250,00 ₽" in summary
    assert "3 строк" in summary


def test_negative_revenue_chart_is_red_horizontal_bars() -> None:
    """График отрицательной выручки — красные горизонтальные бары."""
    result = pd.DataFrame(
        {
            "product": ["A", "B"],
            "revenue": [-200.0, -50.0],
            "rows_count": [1, 2],
            "quantity": [-4.0, -2.0],
        }
    )

    chart = build_chart(_decision("negative_revenue", group_by="product"), result)
    assert chart is not None
    spec = chart.to_dict()
    assert spec["mark"]["color"] == "#d62728"
    assert spec["encoding"]["x"]["field"] == "revenue"
    assert spec["encoding"]["y"]["field"] == "product"


# --------------------------------------------------------------------------- #
# Пустой период и фильтры в сводке
# --------------------------------------------------------------------------- #
def test_empty_summary_shows_available_dates() -> None:
    """Пустой период объясняет, какие даты вообще есть в источнике."""
    empty = pd.DataFrame(columns=["period", "revenue"])

    summary = build_summary(
        _decision("timeseries"),
        empty,
        {"date_min": "2023-01-01", "date_max": "2024-12-31"},
    )

    assert "За выбранный период данных нет" in summary
    assert "данные доступны с 01.01.2023 по 31.12.2024" in summary


def test_summary_mentions_active_filter() -> None:
    """Если сработал product_filter/group_filter, сводка это явно говорит."""
    result = pd.DataFrame({"product": ["P1"], "revenue": [120.0], "share_pct": [100.0]})

    summary = build_summary(
        _decision("top_bottom_products", product_filter="P1", limit=5), result
    )

    assert summary.startswith("По фильтру «P1» найдено 1 записей.")


def test_summary_mentions_group_filter() -> None:
    """group_filter тоже отражается в сводке."""
    result = pd.DataFrame({"product": ["P1"], "revenue": [120.0], "share_pct": [100.0]})

    summary = build_summary(
        _decision("top_bottom_products", group_filter="Бытовая техника", limit=5),
        result,
    )

    assert "По фильтру «Бытовая техника»" in summary


def test_filtered_total_without_rows_explains_no_data() -> None:
    """total с фильтром без совпадений: не «найдено 1 записей… строк 0», а понятное сообщение."""
    result = pd.DataFrame(
        [
            {
                "period_start": pd.Timestamp("2024-01-01"),
                "period_end": pd.Timestamp("2024-12-31"),
                "revenue": 0.0,
                "quantity": 0.0,
                "rows_count": 0,
                "undated_rows": 0,
            }
        ]
    )
    decision = _decision("total", product_filter="НетТакого", start_date="2024-01-01")

    summary = build_summary(
        decision, result, {"date_min": "2023-01-01", "date_max": "2024-12-28"}
    )

    assert summary == "По фильтру «НетТакого» данных не найдено за выбранный период."


def test_format_executed_filter_shows_substitution() -> None:
    """Подмена фильтра при резолюции по каталогу видна в «Технических деталях».

    Диагностическая строка показывает, чем фильтр модели стал при исполнении
    (DECISIONS #30): без неё подмена «Кабель» → «Кабель-удлинитель "Iota"» не
    оставляет следа в UI.
    """
    decision = _decision("top_bottom_products", product_filter="Кабель")

    rendered = format_executed_filter(
        decision, {"product_filter": 'Кабель-удлинитель "Iota"'}
    )

    assert rendered == (
        'Исполнено: фильтр «Кабель-удлинитель "Iota"» (в DSL модели — «Кабель»)'
    )
    assert format_executed_filter(decision, {"product_filter": "Кабель"}) == ""
    assert format_executed_filter(decision, {}) == ""
