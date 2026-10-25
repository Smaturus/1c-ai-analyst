"""Unit-тесты расчётов :mod:`analytics` на синтетической витрине.

Проверяются все пять интентов, фильтр периодов, качество данных и
``dispatch_intent`` — без реальных выгрузок 1С/Excel и без Streamlit.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from analytics import (
    CHECK,
    COUNTERPARTY,
    DATE,
    DELTA,
    DIRECTION,
    INTENT_PARAMETERS,
    INTENT_REGISTRY,
    P1_VALUE,
    P2_VALUE,
    PRODUCT,
    QUANTITY,
    RANK_P1,
    RANK_P2,
    REVENUE,
    ROWS_COUNT,
    STATUS,
    VALUE,
    compare_top_products,
    dispatch_intent,
    filter_by_period,
    get_counterparty_dynamics,
    get_data_quality,
    get_group_dynamics,
    get_timeseries,
    get_top_bottom_products,
    get_total,
)

PERIOD_2023 = ("2023-01-01", "2023-12-31")
PERIOD_2024 = ("2024-01-01", "2024-12-31")


# --------------------------------------------------------------------------- #
# Интент 5: итог за период
# --------------------------------------------------------------------------- #
def test_total_sums_for_period(sales_frame: pd.DataFrame) -> None:
    """Итог за 2024: выручка 180, количество 37, три строки."""
    result = get_total(sales_frame, start_date="2024-01-01", end_date="2024-12-31")

    assert len(result) == 1
    row = result.iloc[0]
    assert row[REVENUE] == pytest.approx(180.0)
    assert row[QUANTITY] == pytest.approx(37.0)
    assert row[ROWS_COUNT] == 3


def test_total_without_bounds_includes_undated(undated_frame: pd.DataFrame) -> None:
    """Без границ периода итог включает строки без даты и честно их считает."""
    row = get_total(undated_frame).iloc[0]

    assert row[REVENUE] == pytest.approx(560.0)
    assert row[QUANTITY] == pytest.approx(75.0)
    assert row["undated_rows"] == 1


def test_total_warns_when_period_drops_undated(undated_frame: pd.DataFrame) -> None:
    """При заданном периоде строки без даты исключаются с предупреждением."""
    with pytest.warns(UserWarning):
        row = get_total(undated_frame, start_date="2024-01-01", end_date="2024-12-31").iloc[0]

    assert row[REVENUE] == pytest.approx(180.0)
    assert row["undated_rows"] == 0


# --------------------------------------------------------------------------- #
# Интент 1: топ/антитоп товаров
# --------------------------------------------------------------------------- #
def test_top_products_sorted_and_with_share(sales_frame: pd.DataFrame) -> None:
    """Топ-2 за 2024: P1 (120, 66.67 %) и P2 (40)."""
    result = get_top_bottom_products(
        sales_frame, limit=2, start_date="2024-01-01", end_date="2024-12-31"
    )

    assert list(result[PRODUCT]) == ["P1", "P2"]
    assert list(result[REVENUE]) == [pytest.approx(120.0), pytest.approx(40.0)]
    assert result.iloc[0]["share_pct"] == pytest.approx(120.0 / 180.0 * 100.0)
    assert list(result["rank"]) == [1, 2]


def test_bottom_products_ascending(sales_frame: pd.DataFrame) -> None:
    """Антитоп-2 за 2024: P3 (20), затем P2 (40)."""
    result = get_top_bottom_products(
        sales_frame,
        limit=2,
        ascending=True,
        start_date="2024-01-01",
        end_date="2024-12-31",
    )

    assert list(result[PRODUCT]) == ["P3", "P2"]


def test_top_products_by_quantity(sales_frame: pd.DataFrame) -> None:
    """Метрика quantity: за 2024 лидер P2 (30 шт.)."""
    result = get_top_bottom_products(
        sales_frame,
        metric=QUANTITY,
        limit=1,
        start_date="2024-01-01",
        end_date="2024-12-31",
    )

    assert result.iloc[0][PRODUCT] == "P2"
    assert result.iloc[0][QUANTITY] == pytest.approx(30.0)


def test_top_products_empty_period_keeps_columns(sales_frame: pd.DataFrame) -> None:
    """Пустой период возвращает пустой DataFrame с теми же колонками."""
    result = get_top_bottom_products(
        sales_frame, start_date="2030-01-01", end_date="2030-12-31"
    )

    assert result.empty
    assert list(result.columns) == ["rank", PRODUCT, REVENUE, "share_pct"]


# --------------------------------------------------------------------------- #
# Интент 3: временной ряд
# --------------------------------------------------------------------------- #
def test_timeseries_monthly_without_gaps(sales_frame: pd.DataFrame) -> None:
    """По месяцам 2023 без заполнения пропусков — три интервала."""
    result = get_timeseries(
        sales_frame,
        freq="M",
        start_date="2023-01-01",
        end_date="2023-12-31",
        fill_gaps=False,
    )

    assert len(result) == 3
    assert list(result[REVENUE]) == [
        pytest.approx(100.0),
        pytest.approx(200.0),
        pytest.approx(30.0),
    ]


def test_timeseries_fills_missing_months(sales_frame: pd.DataFrame) -> None:
    """С fill_gaps=True год достраивается до 12 месяцев с нулями."""
    result = get_timeseries(
        sales_frame,
        freq="M",
        start_date="2023-01-01",
        end_date="2023-12-31",
        fill_gaps=True,
    )

    assert len(result) == 12
    assert result[REVENUE].sum() == pytest.approx(330.0)
    assert (result[REVENUE] == 0).sum() == 9


def test_timeseries_quarterly(sales_frame: pd.DataFrame) -> None:
    """По кварталам 2024 все строки попадают в Q1."""
    result = get_timeseries(
        sales_frame,
        freq="Q",
        start_date="2024-01-01",
        end_date="2024-12-31",
        fill_gaps=False,
    )

    assert len(result) == 1
    assert result.iloc[0][REVENUE] == pytest.approx(180.0)


# --------------------------------------------------------------------------- #
# Интент 2: динамика контрагентов
# --------------------------------------------------------------------------- #
def test_counterparty_dynamics_values(sales_frame: pd.DataFrame) -> None:
    """B — падение на 160, A — рост на 20, C — падение на 10."""
    result = get_counterparty_dynamics(sales_frame, PERIOD_2023, PERIOD_2024)
    by_name = result.set_index(COUNTERPARTY)

    assert by_name.loc["B", DELTA] == pytest.approx(-160.0)
    assert by_name.loc["B", DIRECTION] == "down"
    assert by_name.loc["A", DELTA] == pytest.approx(20.0)
    assert by_name.loc["A", DIRECTION] == "up"
    assert by_name.loc["C", DELTA] == pytest.approx(-10.0)
    assert by_name.loc["A", "delta_pct"] == pytest.approx(20.0)
    assert by_name.loc["B", P1_VALUE] == pytest.approx(200.0)
    assert by_name.loc["B", P2_VALUE] == pytest.approx(40.0)


def test_counterparty_only_drops_keeps_only_falls(sales_frame: pd.DataFrame) -> None:
    """``only_drops=True`` оставляет только падения (B и C), без роста A."""
    result = get_counterparty_dynamics(
        sales_frame, PERIOD_2023, PERIOD_2024, only_drops=True
    )

    assert list(result[COUNTERPARTY]) == ["B", "C"]
    assert (result[DELTA] < 0).all()


def test_counterparty_only_drops_top_n_applies_after_filter(
    sales_frame: pd.DataFrame,
) -> None:
    """``top_n`` ограничивает число падений, а не попадает в общий топ по |delta|."""
    without_filter = get_counterparty_dynamics(
        sales_frame, PERIOD_2023, PERIOD_2024, top_n=2
    )
    with_filter = get_counterparty_dynamics(
        sales_frame, PERIOD_2023, PERIOD_2024, top_n=2, only_drops=True
    )

    assert list(without_filter[COUNTERPARTY]) == ["B", "A"]
    assert list(with_filter[COUNTERPARTY]) == ["B", "C"]


def test_counterparty_direction_new_and_lost() -> None:
    """Контрагент только во втором периоде — ``new``, только в первом — ``lost``."""
    frame = pd.DataFrame(
        [
            ("2023-02-01", "P1", "G1", "Old", 1, 10.0),
            ("2024-02-01", "P1", "G1", "New", 1, 20.0),
        ],
        columns=[DATE, PRODUCT, "product_group", COUNTERPARTY, QUANTITY, REVENUE],
    )
    frame[DATE] = pd.to_datetime(frame[DATE])

    result = get_counterparty_dynamics(frame, PERIOD_2023, PERIOD_2024)
    by_name = result.set_index(COUNTERPARTY)

    assert by_name.loc["Old", DIRECTION] == "lost"
    assert by_name.loc["New", DIRECTION] == "new"


def test_counterparty_metric_quantity(sales_frame: pd.DataFrame) -> None:
    """Метрика quantity считается по количеству, а не по выручке."""
    result = get_counterparty_dynamics(
        sales_frame, PERIOD_2023, PERIOD_2024, metric=QUANTITY, top_n=1
    )

    assert result.iloc[0][COUNTERPARTY] == "B"
    assert result.iloc[0][DELTA] == pytest.approx(30.0 - 20.0)


# --------------------------------------------------------------------------- #
# Интент 4: динамика товарных групп
# --------------------------------------------------------------------------- #
def test_group_dynamics_only_drops(sales_frame: pd.DataFrame) -> None:
    """``only_drops=True`` оставляет G2 (−160) и G3 (−10), но не G1."""
    result = get_group_dynamics(
        sales_frame, PERIOD_2023, PERIOD_2024, group_by="product_group", only_drops=True
    )

    assert list(result["product_group"]) == ["G2", "G3"]
    assert (result[DELTA] < 0).all()


def test_group_dynamics_all_groups(sales_frame: pd.DataFrame) -> None:
    """Без фильтра возвращаются все три группы с направлением изменения."""
    result = get_group_dynamics(sales_frame, PERIOD_2023, PERIOD_2024, group_by="product_group")

    assert set(result["product_group"]) == {"G1", "G2", "G3"}
    assert result.set_index("product_group").loc["G1", DIRECTION] == "up"


def test_group_dynamics_by_product(sales_frame: pd.DataFrame) -> None:
    """``group_by="product"`` группирует по товарам, а не по группам."""
    result = get_group_dynamics(sales_frame, PERIOD_2023, PERIOD_2024, group_by=PRODUCT)

    assert set(result[PRODUCT]) == {"P1", "P2", "P3"}


# --------------------------------------------------------------------------- #
# Фильтр периодов
# --------------------------------------------------------------------------- #
def test_filter_by_period_inclusive(sales_frame: pd.DataFrame) -> None:
    """Границы периода включаются в выборку."""
    result = filter_by_period(sales_frame, "2023-01-15", "2023-01-15")

    assert len(result) == 1
    assert result.iloc[0][PRODUCT] == "P1"


def test_filter_by_period_rejects_reversed_bounds(sales_frame: pd.DataFrame) -> None:
    """Начало позже конца — понятная ошибка, а не пустой результат."""
    with pytest.raises(ValueError, match="позже"):
        filter_by_period(sales_frame, "2024-12-31", "2024-01-01")


def test_filter_by_period_excludes_undated_with_warning(undated_frame: pd.DataFrame) -> None:
    """Строки без даты не попадают в период и не теряются молча."""
    with pytest.warns(UserWarning):
        result = filter_by_period(undated_frame, "2023-01-01", "2024-12-31")

    assert result[DATE].notna().all()
    assert len(result) == 6


# --------------------------------------------------------------------------- #
# Диагностика данных
# --------------------------------------------------------------------------- #
def test_data_quality_counts_undated(undated_frame: pd.DataFrame) -> None:
    """Диагностика показывает одну строку без даты и её долю выручки."""
    quality = get_data_quality(undated_frame).set_index(CHECK)

    assert quality.loc["Строк без даты", VALUE] == 1
    assert quality.loc["Строк с датой", VALUE] == 6
    assert quality.loc["Доля выручки без даты, %", VALUE] == pytest.approx(
        50.0 / 560.0 * 100.0, abs=0.01
    )


# --------------------------------------------------------------------------- #
# dispatch_intent: контракт DSL → расчёт
# --------------------------------------------------------------------------- #
def test_dispatch_intent_runs_counterparty_with_only_drops(sales_frame: pd.DataFrame) -> None:
    """Новый параметр ``only_drops`` проходит через dispatch_intent."""
    result = dispatch_intent(
        "counterparty_dynamics",
        sales_frame,
        {
            "period1": PERIOD_2023,
            "period2": PERIOD_2024,
            "only_drops": True,
        },
    )

    assert list(result[COUNTERPARTY]) == ["B", "C"]


def test_dispatch_intent_rejects_unknown_intent(sales_frame: pd.DataFrame) -> None:
    """Неизвестный интент — ValueError со списком допустимых."""
    with pytest.raises(ValueError, match="Неизвестный интент"):
        dispatch_intent("profit_forecast", sales_frame, {})


def test_dispatch_intent_rejects_unknown_parameter(sales_frame: pd.DataFrame) -> None:
    """Параметр вне контракта интента — ValueError."""
    with pytest.raises(ValueError):
        dispatch_intent("total", sales_frame, {"start_date": date(2024, 1, 1), "limit": 5})


def test_registry_and_parameters_are_in_sync() -> None:
    """Реестр интентов и словарь допустимых параметров описывают одно и то же."""
    assert set(INTENT_REGISTRY) == set(INTENT_PARAMETERS)
    assert "only_drops" in INTENT_PARAMETERS["counterparty_dynamics"]
    assert "compare_top_products" in INTENT_REGISTRY


# --------------------------------------------------------------------------- #
# Интент 6: сравнение топов двух периодов (L4L)
# --------------------------------------------------------------------------- #
def _l4l_frame() -> pd.DataFrame:
    """Витрина для L4L: товар C выпадает, D появляется, A и B меняются местами."""
    return pd.DataFrame(
        [
            ("2023-01-10", "A", "G", "X", 10, 100.0),
            ("2023-01-10", "B", "G", "X", 20, 200.0),
            ("2023-01-10", "C", "G", "X", 30, 300.0),
            ("2024-02-10", "A", "G", "X", 40, 400.0),
            ("2024-02-10", "B", "G", "X", 50, 150.0),
            ("2024-02-10", "D", "G", "X", 60, 500.0),
        ],
        columns=[DATE, PRODUCT, "product_group", COUNTERPARTY, QUANTITY, REVENUE],
    ).assign(**{DATE: lambda frame: pd.to_datetime(frame[DATE])})


def test_compare_top_products_statuses_and_order() -> None:
    """L4L: new/lost/up/down и порядок строк по месту во втором периоде."""
    result = compare_top_products(_l4l_frame(), PERIOD_2023, PERIOD_2024, top_n=3)

    assert list(result.columns) == [
        RANK_P1,
        RANK_P2,
        PRODUCT,
        f"{REVENUE}_p1",
        f"{REVENUE}_p2",
        DELTA,
        STATUS,
    ]
    assert list(result[PRODUCT]) == ["D", "A", "B", "C"]
    assert dict(zip(result[PRODUCT], result[STATUS])) == {
        "D": "new",
        "A": "up",
        "B": "down",
        "C": "lost",
    }
    # Товар D не входил в топ 2023, товар C не вошёл в топ 2024.
    assert pd.isna(result.loc[result[PRODUCT] == "D", RANK_P1]).all()
    assert pd.isna(result.loc[result[PRODUCT] == "C", RANK_P2]).all()


def test_compare_top_products_values_and_delta() -> None:
    """Суммы и delta считаются по периодам; у new/lost delta = NaN (нет базы)."""
    result = compare_top_products(_l4l_frame(), PERIOD_2023, PERIOD_2024, top_n=3)
    by_product = result.set_index(PRODUCT)

    assert by_product.loc["A", f"{REVENUE}_p1"] == 100.0
    assert by_product.loc["A", f"{REVENUE}_p2"] == 400.0
    assert by_product.loc["A", DELTA] == 300.0
    assert by_product.loc["B", DELTA] == -50.0
    assert pd.isna(by_product.loc["D", DELTA])
    assert pd.isna(by_product.loc["C", DELTA])


def test_compare_top_products_respects_top_n() -> None:
    """top_n ограничивает оба рейтинга: при top_n=2 наборы полностью меняются."""
    result = compare_top_products(_l4l_frame(), PERIOD_2023, PERIOD_2024, top_n=2)

    assert len(result) == 4  # B, C (2023) + D, A (2024)
    assert dict(zip(result[PRODUCT], result[STATUS])) == {
        "D": "new",
        "A": "new",
        "B": "lost",
        "C": "lost",
    }


def test_compare_top_products_by_quantity() -> None:
    """Метрика quantity меняет рейтинги и имена колонок, но не логику статусов."""
    result = compare_top_products(
        _l4l_frame(), PERIOD_2023, PERIOD_2024, top_n=3, metric=QUANTITY
    )

    assert f"{QUANTITY}_p1" in result.columns
    assert f"{QUANTITY}_p2" in result.columns
    # По количеству в 2023 лидер C (30), в 2024 — D (60); C выпадает из топа.
    assert list(result[PRODUCT])[:2] == ["D", "B"]
    assert dict(zip(result[PRODUCT], result[STATUS]))["C"] == "lost"


def test_compare_top_products_empty_period_keeps_contract() -> None:
    """Пустые периоды — пустая таблица с ожидаемыми колонками и типами."""
    result = compare_top_products(
        _l4l_frame(), ("2030-01-01", "2030-12-31"), ("2031-01-01", "2031-12-31")
    )

    assert result.empty
    assert list(result.columns) == [
        RANK_P1,
        RANK_P2,
        PRODUCT,
        f"{REVENUE}_p1",
        f"{REVENUE}_p2",
        DELTA,
        STATUS,
    ]
    assert str(result[RANK_P1].dtype) == "Int64"


def test_compare_top_products_via_dispatch() -> None:
    """Интент доступен через dispatch_intent; без второго периода — ValueError."""
    result = dispatch_intent(
        "compare_top_products",
        _l4l_frame(),
        {"period1": PERIOD_2023, "period2": PERIOD_2024, "top_n": 3},
    )
    assert len(result) == 4

    with pytest.raises(ValueError, match="не хватает обязательных параметров"):
        dispatch_intent("compare_top_products", _l4l_frame(), {"period1": PERIOD_2023})
