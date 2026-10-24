"""Аналитические операции MVP: 5 интентов поверх очищенной выгрузки 1С.

Архитектурный принцип системы -- **LLM не считает числа**. Языковая модель
переводит вопрос пользователя в строгий JSON-DSL, а расчёты выполняет этот
модуль на pandas. Результат -- DataFrame, готовый для ``st.dataframe`` /
``st.plotly_chart``, и текст для объяснения.

JSON-DSL (примеры, которые должна возвращать LLM)::

    {"intent": "top_bottom_products",
     "params": {"metric": "revenue", "limit": 10, "ascending": false,
                "start_date": "2024-01-01", "end_date": "2024-12-31"}}

    {"intent": "counterparty_dynamics",
     "params": {"period1": ["2023-01-01", "2023-12-31"],
                "period2": ["2024-01-01", "2024-12-31"],
                "metric": "revenue", "top_n": 10, "only_drops": true}}

    {"intent": "timeseries",
     "params": {"freq": "M", "metric": "revenue"}}

    {"intent": "group_dynamics",
     "params": {"period1": ["2024-01-01", "2024-01-31"],
                "period2": ["2024-02-01", "2024-02-29"],
                "only_drops": true}}

    {"intent": "total",
     "params": {"start_date": "2024-01-01", "end_date": "2024-12-31"}}

Соответствие интентов функциям -- :data:`INTENT_REGISTRY`; его достаточно,
чтобы связать DSL с инструментами LangChain без дополнительных зависимостей.

Общие правила
-------------
* Все функции принимают очищенный DataFrame из
  :func:`data_loader.load_and_clean_1c_report` и возвращают новый DataFrame.
* ``metric`` -- ``"revenue"`` (выручка) или ``"quantity"`` (количество).
* Если задана хотя бы одна граница периода, строки без даты (``NaT``)
  исключаются: их нельзя отнести к периоду.
* Отрицательные значения (возвраты, сторно) не отбрасываются.
* Пустой результат возвращается в виде DataFrame с теми же колонками, чтобы
  вызывающий код (Streamlit) не падал.
"""

from __future__ import annotations

import inspect
import operator
import re
import warnings
from collections.abc import Callable, Sequence
from datetime import date, datetime
from typing import Any, Final, Literal, TypeAlias

import numpy as np
import pandas as pd

from data_loader import COUNTERPARTY, DATE, PRODUCT, QUANTITY, REVENUE

__all__ = [
    "PERIOD",
    "RANK",
    "SHARE_PCT",
    "DELTA",
    "DELTA_PCT",
    "DIRECTION",
    "P1_VALUE",
    "P2_VALUE",
    "PERIOD_START",
    "PERIOD_END",
    "ROWS_COUNT",
    "UNDATED_ROWS",
    "CHECK",
    "VALUE",
    "PRODUCT_GROUP",
    "UNKNOWN_GROUP",
    "TREND_UP",
    "TREND_DOWN",
    "TREND_FLAT",
    "TREND_NEW",
    "TREND_LOST",
    "METRICS",
    "INTENT_REGISTRY",
    "INTENT_PARAMETERS",
    "dispatch_intent",
    "filter_by_period",
    "get_top_bottom_products",
    "get_timeseries",
    "compare_periods",
    "get_counterparty_dynamics",
    "get_group_dynamics",
    "get_total",
    "get_data_quality",
    "derive_product_group",
    "attach_product_group",
]

# --------------------------------------------------------------------------- #
# Типы и константы.
# --------------------------------------------------------------------------- #
DateLike: TypeAlias = str | date | datetime | pd.Timestamp | np.datetime64
"""Допустимое представление даты в параметрах: ISO-строка, ``ДД.ММ.ГГГГ``, ``date``."""

Period: TypeAlias = Sequence[DateLike]
"""Пара (начало, конец) включительно: кортеж, список, массив или Series длины 2."""

MetricName: TypeAlias = Literal["revenue", "quantity"]
AggMode: TypeAlias = Literal["sum", "mean", "count"]
SortBy: TypeAlias = Literal["delta", "delta_abs", "period1_value", "period2_value", "name"]

PERIOD: Final[str] = "period"
"""Колонка с началом временного интервала (временной ряд)."""

RANK: Final[str] = "rank"
SHARE_PCT: Final[str] = "share_pct"
"""Доля строки в общем итоге, %."""

P1_VALUE: Final[str] = "period1_value"
P2_VALUE: Final[str] = "period2_value"
DELTA: Final[str] = "delta"
DELTA_PCT: Final[str] = "delta_pct"
"""Изменение в процентах: ``(period2 - period1) / period1 * 100``; ``NaN`` при ``period1 <= 0``."""

DIRECTION: Final[str] = "direction"
PRODUCT_GROUP: Final[str] = "product_group"
PERIOD_START: Final[str] = "period_start"
PERIOD_END: Final[str] = "period_end"
ROWS_COUNT: Final[str] = "rows_count"
UNDATED_ROWS: Final[str] = "undated_rows"
CHECK: Final[str] = "check"
VALUE: Final[str] = "value"

TREND_UP: Final[str] = "up"
TREND_DOWN: Final[str] = "down"
TREND_FLAT: Final[str] = "flat"
TREND_NEW: Final[str] = "new"
"""Контрагент/группа появился во втором периоде (в первом продаж не было)."""

TREND_LOST: Final[str] = "lost"
"""Контрагент/группа пропал во втором периоде (продажи были, стали нулевыми)."""

METRICS: Final[tuple[str, ...]] = (REVENUE, QUANTITY)
"""Доступные метрики расчёта."""

UNKNOWN_GROUP: Final[str] = "Прочее"
"""Группа для товаров, у которых не удалось выделить категорию."""

# Псевдонимы частот: pandas 2.x отдаёт предпочтение "ME"/"QE"/"YE" для
# ресемплинга, а для Period (to_period) валидны "M"/"Q"/"Y" в обеих версиях.
# Приводим пользовательский ввод к алиасу Period.
_PERIOD_ALIASES: Final[dict[str, str]] = {
    "D": "D",
    "W": "W",
    "M": "M",
    "ME": "M",
    "MS": "M",
    "Q": "Q",
    "QE": "Q",
    "QS": "Q",
    "Y": "Y",
    "YE": "Y",
    "YS": "Y",
    "A": "Y",
}

# Первое слово названия товара как товарная группа: "Вентилятор BINATONE ... " ->
# "Вентилятор". Пунктуация по краям токена срезается.
_GROUP_TOKEN_CLEANUP: Final[re.Pattern[str]] = re.compile(r"^\W+|\W+$")

_TRUE_STRINGS: Final[frozenset[str]] = frozenset({"true", "1", "yes", "y", "да"})
_FALSE_STRINGS: Final[frozenset[str]] = frozenset({"false", "0", "no", "n", "нет"})


# --------------------------------------------------------------------------- #
# Публичные точки входа: 5 интентов MVP.
# --------------------------------------------------------------------------- #
def get_top_bottom_products(
    df: pd.DataFrame,
    metric: MetricName = REVENUE,
    limit: int = 10,
    ascending: bool = False,
    *,
    start_date: DateLike | None = None,
    end_date: DateLike | None = None,
) -> pd.DataFrame:
    """Интент 1. Лучшие или худшие товары за период.

    Parameters
    ----------
    df:
        Очищенный DataFrame из :func:`data_loader.load_and_clean_1c_report`.
    metric:
        ``"revenue"`` или ``"quantity"``.
    limit:
        Сколько строк вернуть; должно быть положительным.
    ascending:
        ``False`` -- лучшие товары (сортировка по убыванию),
        ``True`` -- худшие (по возрастанию).
    start_date, end_date:
        Необязательные границы периода включительно.

    Returns
    -------
    pandas.DataFrame
        Колонки ``rank``, ``product``, ``<metric>``, ``share_pct``.
        ``rank`` = 1 -- самый крупный (или самый слабый при ``ascending=True``)
        товар; ``share_pct`` -- доля в общем итоге по всем товарам, %.

    Examples
    --------
    >>> get_top_bottom_products(df, limit=5)                    # doctest: +SKIP
    >>> get_top_bottom_products(df, limit=5, ascending=True)    # doctest: +SKIP
    """
    metric = _check_metric(metric)
    limit = _positive_int(limit, "limit")
    ascending = _as_bool(ascending, "ascending")
    scoped = filter_by_period(df, start_date, end_date)
    if scoped.empty:
        return _empty_frame((RANK, PRODUCT, metric, SHARE_PCT))

    grouped = scoped.groupby(PRODUCT, dropna=False)[metric].sum()
    total = float(grouped.sum())

    table = grouped.rename(metric).reset_index()
    table = table.sort_values(
        [metric, PRODUCT],
        ascending=[bool(ascending), True],
        kind="stable",
    ).head(limit)
    table = table.reset_index(drop=True)
    table.insert(0, RANK, range(1, len(table) + 1))
    table[SHARE_PCT] = (table[metric] / total * 100.0) if total else np.nan
    return table.loc[:, [RANK, PRODUCT, metric, SHARE_PCT]]


def get_timeseries(
    df: pd.DataFrame,
    freq: str = "M",
    *,
    metric: MetricName = REVENUE,
    agg: AggMode = "sum",
    start_date: DateLike | None = None,
    end_date: DateLike | None = None,
    fill_gaps: bool = True,
) -> pd.DataFrame:
    """Интент 3. Временной ряд продаж с заданной частотой.

    Parameters
    ----------
    df:
        Очищенный DataFrame.
    freq:
        Частота: ``"D"`` -- день, ``"W"`` -- неделя, ``"M"`` -- месяц (по
        умолчанию), ``"Q"`` -- квартал, ``"Y"`` -- год. Принимаются и
        псевдонимы pandas (``"ME"``, ``"QE"`` и т.п.).
    metric:
        ``"revenue"`` или ``"quantity"``.
    agg:
        Агрегация внутри интервала: ``"sum"`` (по умолчанию), ``"mean"``,
        ``"count"``. При ``"count"`` в колонке ``<metric>`` лежит число записей
        интервала (``int64``), а не сумма или среднее.
    start_date, end_date:
        Необязательные границы периода включительно.
    fill_gaps:
        Заполнять ли пропущенные интервалы нулями (только для ``agg="sum"``,
        чтобы линия на графике не разрывалась).

    Returns
    -------
    pandas.DataFrame
        Колонки ``period`` (начало интервала, ``datetime64``) и ``<metric>``.
        Строки без даты в расчёт не попадают.

    Examples
    --------
    >>> get_timeseries(df, freq="M")                            # doctest: +SKIP
    >>> get_timeseries(df, freq="Q", start_date="2024-01-01")   # doctest: +SKIP
    """
    metric = _check_metric(metric)
    if agg not in ("sum", "mean", "count"):
        raise ValueError(f"agg должен быть 'sum', 'mean' или 'count', получено {agg!r}")
    fill_gaps = _as_bool(fill_gaps, "fill_gaps")
    period_alias = _period_alias(freq)

    scoped = filter_by_period(df, start_date, end_date)
    scoped = scoped.loc[scoped[DATE].notna()]
    if scoped.empty:
        return _empty_frame((PERIOD, metric))

    buckets = scoped[DATE].dt.to_period(period_alias).dt.start_time
    series = scoped.groupby(buckets)[metric].agg(agg).sort_index()

    if fill_gaps and agg == "sum" and len(series) > 0:
        range_start = (
            _to_timestamp(start_date) if start_date is not None else series.index.min()
        )
        range_end = (
            _to_timestamp(end_date) if end_date is not None else series.index.max()
        )
        full_range = pd.period_range(
            range_start,
            range_end,
            freq=period_alias,
        ).start_time
        series = series.reindex(full_range, fill_value=0.0)

    result = series.rename(metric).reset_index()
    result.columns = [PERIOD, metric]
    return result


def compare_periods(
    df: pd.DataFrame,
    dim: str,
    period1: Period,
    period2: Period,
    *,
    metric: MetricName = REVENUE,
    top_n: int | None = None,
    min_abs_delta: float | None = None,
    sort_by: SortBy = "delta_abs",
) -> pd.DataFrame:
    """Сравнить две произвольные группы (измерения) между двумя периодами.

    Универсальное ядро интентов 2 и 4: считает суммы метрики по значению
    измерения в каждом периоде, дельту в абсолютных единицах и процентах.

    Parameters
    ----------
    df:
        Очищенный DataFrame (или DataFrame с дополнительной колонкой-группой).
    dim:
        Имя колонки-измерения: ``"counterparty"``, ``"product"``,
        ``"product_group"`` и т.п.
    period1, period2:
        Пары дат ``(начало, конец)`` включительно; каждая дата -- ISO-строка,
        строка ``ДД.ММ.ГГГГ``, ``date`` или ``Timestamp``.
    metric:
        ``"revenue"`` или ``"quantity"``.
    top_n:
        Оставить N строк после сортировки.
    min_abs_delta:
        Оставить только строки, где ``|delta| >= min_abs_delta``.
    sort_by:
        Порядок строк: ``"delta_abs"`` (по модулю изменения, по умолчанию),
        ``"delta"``, ``"period2_value"``, ``"period1_value"``, ``"name"``.

    Returns
    -------
    pandas.DataFrame
        Колонки ``<dim>``, ``period1_value``, ``period2_value``, ``delta``,
        ``delta_pct``, ``direction``. Значения ``direction``:
        ``up`` / ``down`` / ``flat`` / ``new`` / ``lost``.
        ``delta_pct`` = ``NaN``, если в первом периоде не было положительной базы
        (нулевые или отрицательные продажи -- проценты от такой базы бессмысленны).

    Raises
    ------
    ValueError
        Неизвестная метрика, отсутствующая колонка-измерение или период,
        у которого начало позже конца.
    """
    metric = _check_metric(metric)
    _ensure_columns(df, (DATE, metric))
    if dim not in df.columns:
        raise ValueError(
            f"Колонка-измерение {dim!r} отсутствует. Доступно: {list(df.columns)}. "
            f"Для группировки по товарной группе вызовите attach_product_group(df)."
        )
    if sort_by not in ("delta", "delta_abs", "period1_value", "period2_value", "name"):
        raise ValueError(f"Неизвестный порядок сортировки sort_by={sort_by!r}")
    if top_n is not None:
        top_n = _positive_int(top_n, "top_n")
    if min_abs_delta is not None:
        min_abs_delta = float(min_abs_delta)
        if min_abs_delta < 0:
            raise ValueError(f"min_abs_delta не может быть отрицательным: {min_abs_delta}")

    p1_start, p1_end = _as_period(period1, "period1")
    p2_start, p2_end = _as_period(period2, "period2")
    if p1_start > p2_start:
        warnings.warn(
            f"period1 начинается позже period2 ({p1_start.date()} > {p2_start.date()}): "
            "проверьте порядок периодов.",
            UserWarning,
            stacklevel=2,
        )

    first = _sum_by_dimension(df, dim, metric, p1_start, p1_end).rename(P1_VALUE)
    second = _sum_by_dimension(df, dim, metric, p2_start, p2_end).rename(P2_VALUE)
    if first.empty and second.empty:
        return _empty_frame((dim, P1_VALUE, P2_VALUE, DELTA, DELTA_PCT, DIRECTION))

    table = pd.concat([first, second], axis=1).fillna(0.0)
    table[DELTA] = table[P2_VALUE] - table[P1_VALUE]
    table[DELTA_PCT] = _percent_change(table[P1_VALUE], table[DELTA])
    table[DIRECTION] = _classify_direction(table[P1_VALUE], table[P2_VALUE], table[DELTA])

    table = _sort_dynamics(table, dim=dim, sort_by=sort_by)

    if min_abs_delta is not None:
        table = table.loc[table[DELTA].abs() >= min_abs_delta]
    if top_n is not None:
        table = table.head(top_n)

    table = table.reset_index()
    return table.loc[:, [dim, P1_VALUE, P2_VALUE, DELTA, DELTA_PCT, DIRECTION]]


def get_counterparty_dynamics(
    df: pd.DataFrame,
    period1: Period,
    period2: Period,
    *,
    metric: MetricName = REVENUE,
    top_n: int | None = None,
    min_abs_delta: float | None = None,
    sort_by: SortBy = "delta_abs",
    only_drops: bool = False,
) -> pd.DataFrame:
    """Интент 2. У каких контрагентов выросли или упали продажи.

    Тонкая обёртка над :func:`compare_periods` с измерением ``counterparty``:
    отвечает на вопрос «кто дал прирост/падение между двумя периодами».

    Parameters
    ----------
    df:
        Очищенный DataFrame.
    period1, period2:
        Сравниваемые периоды ``(начало, конец)`` включительно.
    metric:
        ``"revenue"`` или ``"quantity"``.
    top_n, min_abs_delta, sort_by:
        См. :func:`compare_periods`.
    only_drops:
        Оставить только контрагентов с падением (``delta < 0``) — ответ на
        вопрос «у каких контрагентов упали продажи». Фильтр применяется до
        ``top_n``, то есть ``top_n`` ограничивает число самых крупных падений.

    Returns
    -------
    pandas.DataFrame
        ``counterparty``, ``period1_value``, ``period2_value``, ``delta``,
        ``delta_pct``, ``direction``.

    Examples
    --------
    >>> get_counterparty_dynamics(                                # doctest: +SKIP
    ...     df, ("2023-01-01", "2023-12-31"), ("2024-01-01", "2024-12-31"),
    ...     only_drops=True,
    ... )
    """
    only_drops = _as_bool(only_drops, "only_drops")
    if top_n is not None:
        top_n = _positive_int(top_n, "top_n")
    result = compare_periods(
        df,
        COUNTERPARTY,
        period1,
        period2,
        metric=metric,
        top_n=None if only_drops else top_n,
        min_abs_delta=min_abs_delta,
        sort_by=sort_by,
    )
    if only_drops and not result.empty:
        result = result.loc[result[DELTA] < 0].reset_index(drop=True)
        if top_n is not None:
            result = result.head(top_n).reset_index(drop=True)
    return result


def get_group_dynamics(
    df: pd.DataFrame,
    period1: Period,
    period2: Period,
    *,
    group_by: str | pd.Series | Callable[[pd.Series], pd.Series] | None = None,
    metric: MetricName = REVENUE,
    top_n: int | None = None,
    min_abs_delta: float | None = None,
    sort_by: SortBy = "delta_abs",
    only_drops: bool = False,
) -> pd.DataFrame:
    """Интент 4. Срез по товарным группам: что просело, а что выросло.

    В выгрузке «Универсального отчёта» нет отдельной колонки «товарная
    группа», поэтому по умолчанию группа либо берётся из готовой колонки
    ``product_group`` (её даёт :mod:`onec_source` -- иерархия справочника
    «Номенклатура»), либо выводится эвристикой :func:`derive_product_group`
    (первое слово названия товара). При появлении настоящего справочника групп
    достаточно передать имя колонки или готовую серию -- сигнатура функции не
    изменится.

    Parameters
    ----------
    df:
        Очищенный DataFrame.
    period1, period2:
        Сравниваемые периоды ``(начало, конец)`` включительно.
    group_by:
        Чем группировать:

        * ``None`` -- товарная группа: готовая колонка ``product_group`` (если
          она есть, например из :mod:`onec_source`) или эвристика по названию;
        * ``str`` -- имя существующей колонки (например, ``"product"``);
        * ``pandas.Series`` -- готовые группы, выровненные по индексу ``df``;
        * ``callable`` -- функция ``Series[product] -> Series[group]``.
    metric:
        ``"revenue"`` или ``"quantity"``.
    top_n, min_abs_delta, sort_by:
        См. :func:`compare_periods`.
    only_drops:
        Оставить только группы с падением (``delta < 0``). Фильтр применяется до
        ``top_n``, то есть ``top_n`` ограничивает число самых крупных падений.

    Returns
    -------
    pandas.DataFrame
        ``<dim>``, ``period1_value``, ``period2_value``, ``delta``,
        ``delta_pct``, ``direction``.

    Examples
    --------
    >>> get_group_dynamics(                                       # doctest: +SKIP
    ...     df, ("2024-01-01", "2024-01-31"), ("2024-02-01", "2024-02-29"),
    ...     only_drops=True,
    ... )
    """
    prepared, dim = _prepare_group_column(df, group_by)
    only_drops = _as_bool(only_drops, "only_drops")
    if top_n is not None:
        top_n = _positive_int(top_n, "top_n")
    result = compare_periods(
        prepared,
        dim,
        period1,
        period2,
        metric=metric,
        top_n=None if only_drops else top_n,
        min_abs_delta=min_abs_delta,
        sort_by=sort_by,
    )
    if only_drops and not result.empty:
        result = result.loc[result[DELTA] < 0].reset_index(drop=True)
        if top_n is not None:
            result = result.head(top_n).reset_index(drop=True)
    return result


def get_total(
    df: pd.DataFrame,
    *,
    start_date: DateLike | None = None,
    end_date: DateLike | None = None,
) -> pd.DataFrame:
    """Интент 5. Сколько продали за период (одна строка итогов).

    Parameters
    ----------
    df:
        Очищенный DataFrame.
    start_date, end_date:
        Необязательные границы периода включительно. Если задана только одна
        граница, вторая подставляется по фактическим данным (min/max даты в
        выборке). Если границы не заданы вовсе, считаются все строки файла,
        включая строки без даты (см. ``undated_rows``).

    Returns
    -------
    pandas.DataFrame
        Одна строка: ``period_start``, ``period_end``, ``revenue``,
        ``quantity``, ``rows_count``, ``undated_rows``.
        ``undated_rows`` -- сколько строк без даты попало в расчёт (важно для
        честного объяснения результата).

    Examples
    --------
    >>> get_total(df, start_date="2024-01-01", end_date="2024-12-31")  # doctest: +SKIP
    """
    _ensure_columns(df, (DATE, REVENUE, QUANTITY))
    scoped = filter_by_period(df, start_date, end_date)

    if start_date is not None:
        start = _to_timestamp(start_date)
    else:
        start = scoped[DATE].min() if not scoped.empty else pd.NaT
    if end_date is not None:
        end = _to_timestamp(end_date)
    else:
        end = scoped[DATE].max() if not scoped.empty else pd.NaT
    row = {
        PERIOD_START: start,
        PERIOD_END: end,
        REVENUE: float(scoped[REVENUE].sum()),
        QUANTITY: float(scoped[QUANTITY].sum()),
        ROWS_COUNT: int(len(scoped)),
        UNDATED_ROWS: int(scoped[DATE].isna().sum()),
    }
    return pd.DataFrame([row], columns=[PERIOD_START, PERIOD_END, REVENUE, QUANTITY, ROWS_COUNT, UNDATED_ROWS])


# --------------------------------------------------------------------------- #
# Вспомогательные публичные функции.
# --------------------------------------------------------------------------- #
def filter_by_period(
    df: pd.DataFrame,
    start_date: DateLike | None = None,
    end_date: DateLike | None = None,
) -> pd.DataFrame:
    """Оставить строки, дата которых попадает в период включительно.

    Если не задана ни одна граница, возвращается исходный DataFrame целиком --
    вместе со строками без даты. Если задана хотя бы одна граница, строки с
    ``NaT`` исключаются (их нельзя отнести к периоду).

    Parameters
    ----------
    df:
        Очищенный DataFrame.
    start_date, end_date:
        Границы периода; ``None`` -- без ограничения с этой стороны.

    Returns
    -------
    pandas.DataFrame
        Подмножество строк (или исходный объект, если фильтр не задан).

    Raises
    ------
    ValueError
        Если ``start_date`` позже ``end_date`` или дату не удалось разобрать.
    """
    _ensure_columns(df, (DATE,))
    if start_date is None and end_date is None:
        return df

    start = _to_timestamp(start_date) if start_date is not None else None
    end = _to_timestamp(end_date) if end_date is not None else None
    if start is not None and end is not None and start > end:
        raise ValueError(
            f"start_date ({start.date()}) позже end_date ({end.date()}): "
            "проверьте границы периода."
        )

    mask = df[DATE].notna()
    if start is not None:
        mask &= df[DATE] >= start
    if end is not None:
        mask &= df[DATE] <= end

    undated = df[DATE].isna()
    undated_count = int(undated.sum())
    if undated_count:
        undated_revenue = float(df.loc[undated, REVENUE].sum()) if REVENUE in df.columns else 0.0
        warnings.warn(
            f"Из расчёта исключено {undated_count} строк без даты "
            f"(выручка {undated_revenue:.2f}): их нельзя отнести к периоду. "
            "Полный итог по файлу возвращает get_total() без границ периода.",
            UserWarning,
            stacklevel=2,
        )
    return df.loc[mask]


def derive_product_group(products: pd.Series) -> pd.Series:
    """Вывести товарную группу из названия товара (эвристика MVP).

    Берётся первое слово названия: ``"Вентилятор BINATONE ALPINE 160вт"`` ->
    ``"Вентилятор"``. Хвостовая пунктуация срезается, пустые значения
    попадают в :data:`UNKNOWN_GROUP`.

    Это замена отсутствующей в выгрузке колонки «товарная группа». Когда
    появится настоящий справочник, функцию можно не использовать и передать в
    :func:`get_group_dynamics` реальную колонку групп.

    Parameters
    ----------
    products:
        Серия названий товаров.

    Returns
    -------
    pandas.Series
        Серия групп (``string``), индекс совпадает с исходным.
    """
    text = products.astype("string").str.strip().fillna("")
    token = text.str.split(r"\s+", n=1).str[0]
    token = token.str.replace(_GROUP_TOKEN_CLEANUP, "", regex=True)
    token = token.mask(token.eq(""), UNKNOWN_GROUP)
    return token.astype("string")


def attach_product_group(df: pd.DataFrame) -> pd.DataFrame:
    """Вернуть копию DataFrame с колонкой ``product_group``.

    Если колонка ``product_group`` уже есть (например, пришла из
    :mod:`onec_source` -- иерархия справочника «Номенклатура»), она
    сохраняется: заполняются только пустые значения, и для них используется
    эвристика :func:`derive_product_group`.

    Parameters
    ----------
    df:
        Очищенный DataFrame (из ``data_loader`` или ``onec_source``).

    Returns
    -------
    pandas.DataFrame
        Копия с заполненной колонкой ``product_group``.

    Examples
    --------
    >>> grouped = attach_product_group(df)                        # doctest: +SKIP
    >>> grouped.groupby("product_group")["revenue"].sum()          # doctest: +SKIP
    """
    _ensure_columns(df, (PRODUCT,))
    result = df.copy()
    heuristic = derive_product_group(result[PRODUCT])
    if PRODUCT_GROUP in result.columns:
        existing = result[PRODUCT_GROUP].astype("string")
        missing = existing.isna() | existing.str.strip().eq("")
        result[PRODUCT_GROUP] = existing.mask(missing, heuristic)
    else:
        result[PRODUCT_GROUP] = heuristic
    return result


def get_data_quality(df: pd.DataFrame) -> pd.DataFrame:
    """Диагностика полноты данных -- для баннера в Streamlit и объяснений LLM.

    Показывает, сколько строк и выручки не имеет даты (такие строки не
    попадают в аналитику по периодам), сколько возвратов и нулевых строк.

    Parameters
    ----------
    df:
        Очищенный DataFrame.

    Returns
    -------
    pandas.DataFrame
        Колонки ``check`` (текст проверки) и ``value`` (значение).
    """
    _ensure_columns(df, (DATE, REVENUE, QUANTITY))
    total_revenue = float(df[REVENUE].sum())
    undated = df[DATE].isna()
    undated_revenue = float(df.loc[undated, REVENUE].sum())
    dated = ~undated

    checks: list[dict[str, object]] = [
        {"check": "Всего строк", "value": int(len(df))},
        {"check": "Строк с датой", "value": int(dated.sum())},
        {"check": "Строк без даты", "value": int(undated.sum())},
        {
            "check": "Доля выручки без даты, %",
            "value": round(undated_revenue / total_revenue * 100.0, 2) if total_revenue else 0.0,
        },
        {"check": "Выручка без даты", "value": round(undated_revenue, 2)},
        {"check": "Строк с нулевой выручкой", "value": int((df[REVENUE] == 0).sum())},
        {"check": "Строк с отрицательной выручкой", "value": int((df[REVENUE] < 0).sum())},
        {"check": "Минимальная дата", "value": _date_to_iso(df[DATE].min())},
        {"check": "Максимальная дата", "value": _date_to_iso(df[DATE].max())},
    ]
    return pd.DataFrame(checks, columns=[CHECK, VALUE])


# --------------------------------------------------------------------------- #
# Реестр интентов: связка JSON-DSL -> функция (для LangChain-инструментов).
# --------------------------------------------------------------------------- #
INTENT_REGISTRY: Final[dict[str, Callable[..., pd.DataFrame]]] = {
    "top_bottom_products": get_top_bottom_products,
    "timeseries": get_timeseries,
    "counterparty_dynamics": get_counterparty_dynamics,
    "group_dynamics": get_group_dynamics,
    "total": get_total,
}
"""Имя интента из JSON-DSL -> функция расчёта (см. docstring модуля)."""

INTENT_PARAMETERS: Final[dict[str, tuple[str, ...]]] = {
    "top_bottom_products": ("metric", "limit", "ascending", "start_date", "end_date"),
    "timeseries": ("freq", "metric", "agg", "start_date", "end_date", "fill_gaps"),
    "counterparty_dynamics": (
        "period1",
        "period2",
        "metric",
        "top_n",
        "min_abs_delta",
        "sort_by",
        "only_drops",
    ),
    "group_dynamics": (
        "period1",
        "period2",
        "group_by",
        "metric",
        "top_n",
        "min_abs_delta",
        "sort_by",
        "only_drops",
    ),
    "total": ("start_date", "end_date"),
}
"""Допустимые параметры каждого интента -- основа схемы инструмента для LLM."""


def dispatch_intent(
    intent: str,
    df: pd.DataFrame,
    params: dict[str, Any] | None = None,
) -> pd.DataFrame:
    """Выполнить интент по имени из JSON-DSL.

    Это точка входа для слоя интеграции (Streamlit / LangChain): LLM отдаёт
    ``{"intent": ..., "params": {...}}``, здесь проверяются имя интента и состав
    параметров, а расчёт делегируется нужной функции.

    Parameters
    ----------
    intent:
        Имя интента, ключ :data:`INTENT_REGISTRY`.
    df:
        Очищенный DataFrame.
    params:
        Параметры интента (значения из JSON-DSL). ``None`` -- параметры по
        умолчанию.

    Returns
    -------
    pandas.DataFrame
        Результат соответствующей функции.

    Raises
    ------
    ValueError
        Неизвестный интент или лишний/неподдерживаемый параметр.

    Examples
    --------
    >>> dispatch_intent(                                          # doctest: +SKIP
    ...     "top_bottom_products",
    ...     df,
    ...     {"metric": "revenue", "limit": 5, "ascending": False},
    ... )
    """
    handler = INTENT_REGISTRY.get(intent)
    if handler is None:
        raise ValueError(
            f"Неизвестный интент {intent!r}. Допустимо: {sorted(INTENT_REGISTRY)}"
        )
    if params is not None and not isinstance(params, dict):
        raise ValueError(
            f"params интента {intent!r} должен быть объектом JSON (dict), "
            f"получено {type(params)!r}"
        )

    arguments = dict(params or {})
    allowed = INTENT_PARAMETERS[intent]
    unknown = sorted(set(arguments) - set(allowed))
    if unknown:
        raise ValueError(
            f"Интент {intent!r} не принимает параметры {unknown}. Допустимо: {list(allowed)}"
        )
    missing = sorted(_required_parameters(intent) - set(arguments))
    if missing:
        raise ValueError(
            f"Интенту {intent!r} не хватает обязательных параметров: {missing}"
        )
    return handler(df, **arguments)


def _required_parameters(intent: str) -> frozenset[str]:
    """Имена обязательных параметров функции интента (без ``df``)."""
    signature = inspect.signature(INTENT_REGISTRY[intent])
    return frozenset(
        name
        for name, parameter in signature.parameters.items()
        if name != "df"
        and parameter.default is inspect.Parameter.empty
        and parameter.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    )


# --------------------------------------------------------------------------- #
# Приватные помощники.
# --------------------------------------------------------------------------- #
def _ensure_columns(df: pd.DataFrame, required: Sequence[str]) -> None:
    """Проверить, что в DataFrame есть обязательные колонки."""
    missing = [column for column in required if column not in df.columns]
    if missing:
        raise ValueError(
            f"В DataFrame нет обязательных колонок {missing}. "
            f"Ожидается результат load_and_clean_1c_report; фактически: {list(df.columns)}"
        )


def _check_metric(metric: str) -> str:
    """Проверить, что метрика поддерживается."""
    if metric not in METRICS:
        raise ValueError(f"metric должен быть одним из {list(METRICS)}, получено {metric!r}")
    return metric


def _percent_change(previous: pd.Series, delta: pd.Series) -> pd.Series:
    """Рассчитать изменение в процентах от положительной базы прошлого периода.

    Процент определён только при ``previous > 0``. Если в прошлом периоде была
    нулевая или отрицательная величина (например, только возвраты), процент
    вернуть нельзя: ``NaN`` честнее, чем «рост на +100%» от отрицательной базы.
    В таблице такие строки показываются как «—», а направление
    (``new``/``lost``/``up``/``down``) остаётся в колонке ``direction``.
    """
    previous_values = previous.to_numpy(dtype="float64")
    delta_values = delta.to_numpy(dtype="float64")
    with np.errstate(divide="ignore", invalid="ignore"):
        result = np.where(
            previous_values > 0.0,
            delta_values / previous_values * 100.0,
            np.nan,
        )
    return pd.Series(result, index=previous.index, dtype="float64")


def _classify_direction(
    previous: pd.Series,
    current: pd.Series,
    delta: pd.Series,
) -> pd.Series:
    """Классифицировать изменение: up / down / flat / new / lost."""
    previous_values = previous.to_numpy(dtype="float64")
    current_values = current.to_numpy(dtype="float64")
    delta_values = delta.to_numpy(dtype="float64")

    conditions = [
        (previous_values == 0.0) & (current_values != 0.0),
        (previous_values != 0.0) & (current_values == 0.0),
        delta_values > 0.0,
        delta_values < 0.0,
    ]
    choices = [TREND_NEW, TREND_LOST, TREND_UP, TREND_DOWN]
    directions = np.select(conditions, choices, default=TREND_FLAT)
    return pd.Series(directions, index=previous.index, dtype="string")


def _sort_dynamics(table: pd.DataFrame, *, dim: str, sort_by: SortBy) -> pd.DataFrame:
    """Отсортировать таблицу динамики по выбранному правилу."""
    if sort_by == "name":
        return table.sort_index(kind="stable")
    if sort_by == "delta_abs":
        key = table[DELTA].abs()
    else:
        key = table[sort_by]
    augmented = table.assign(_sort_key=key)
    augmented = augmented.sort_values(
        ["_sort_key", dim],
        ascending=[False, True],
        kind="stable",
    )
    return augmented.drop(columns="_sort_key")


def _sum_by_dimension(
    df: pd.DataFrame,
    dim: str,
    metric: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.Series:
    """Сумма метрики по измерению за период включительно."""
    mask = df[DATE].notna() & (df[DATE] >= start) & (df[DATE] <= end)
    return df.loc[mask].groupby(dim, dropna=False)[metric].sum()


def _prepare_group_column(
    df: pd.DataFrame,
    group_by: str | pd.Series | Callable[[pd.Series], pd.Series] | None,
) -> tuple[pd.DataFrame, str]:
    """Подготовить DataFrame и имя колонки-группы для :func:`get_group_dynamics`."""
    if group_by is None:
        return attach_product_group(df), PRODUCT_GROUP

    if isinstance(group_by, str):
        if group_by not in df.columns:
            raise ValueError(
                f"Колонка {group_by!r} отсутствует. Доступно: {list(df.columns)}. "
                "Для вывода товарной группы из названия используйте group_by=None."
            )
        return df, group_by

    if isinstance(group_by, pd.Series):
        prepared = df.copy()
        prepared[PRODUCT_GROUP] = _aligned_group_values(group_by, df)
        return prepared, PRODUCT_GROUP

    if callable(group_by):
        _ensure_columns(df, (PRODUCT,))
        prepared = df.copy()
        groups = group_by(prepared[PRODUCT])
        prepared[PRODUCT_GROUP] = _coerce_group_values(groups, len(df), "результат callable")
        return prepared, PRODUCT_GROUP

    raise TypeError(
        "group_by должен быть None, именем колонки, pandas.Series или callable, "
        f"получено {type(group_by)!r}"
    )


def _coerce_group_values(
    groups: pd.Series | Sequence[object],
    expected_length: int,
    source: str,
) -> np.ndarray:
    """Привести значения групп к массиву нужной длины и проверить размер.

    Принимает ``pandas.Series`` (индекс не важен) или любой sequence/array,
    который вернул пользовательский ``callable``.
    """
    if isinstance(groups, pd.Series):
        values = groups.to_numpy()
    else:
        values = np.asarray(groups, dtype=object)
    if values.ndim != 1:
        raise TypeError(
            f"{source} должен вернуть одномерную последовательность групп, "
            f"получено {type(groups)!r}"
        )
    if len(values) != expected_length:
        raise ValueError(
            f"{source} вернул {len(values)} значений, а в DataFrame {expected_length} строк"
        )
    return values


def _aligned_group_values(groups: pd.Series, df: pd.DataFrame) -> np.ndarray:
    """Выровнять переданную Series групп по индексу DataFrame.

    Порядок значений в Series не должен влиять на результат: если индексы
    совпадают, значения переставляются в порядке строк DataFrame. Если индекс
    выровнять нельзя (например, DataFrame с неуникальным индексом), значения
    подставляются позиционно с предупреждением.
    """
    if len(groups) != len(df):
        raise ValueError(
            f"Серия групп имеет длину {len(groups)}, а DataFrame -- {len(df)}"
        )

    missing_labels = df.index.difference(groups.index)
    if len(missing_labels):
        raise ValueError(
            "Индекс переданной Series групп не покрывает индекс DataFrame: "
            f"нет значений для {len(missing_labels)} строк. "
            "Передайте Series с тем же индексом (df.index)."
        )

    try:
        aligned = groups.reindex(df.index)
    except (ValueError, TypeError) as exc:  # неуникальный индекс и т.п.
        warnings.warn(
            "Индекс Series групп не выровнять по индексу DataFrame: "
            f"значения подставлены по позициям ({exc}).",
            UserWarning,
            stacklevel=3,
        )
        return groups.to_numpy()
    return aligned.to_numpy()


def _date_to_iso(value: object) -> str | None:
    """Преобразовать дату в ISO-строку (или ``None``), чтобы результат был JSON-safe."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):  # pragma: no cover - неожиданный тип значения
        return None
    return pd.Timestamp(value).strftime("%Y-%m-%d")


def _positive_int(value: object, name: str) -> int:
    """Проверить, что значение -- целое положительное число.

    Принимает ``int`` и целочисленные типы numpy, но не ``bool`` и не ``float``:
    молчаливое ``head(2.5)`` дало бы непонятную ошибку глубже в pandas.
    """
    if isinstance(value, (bool, np.bool_)):
        raise ValueError(f"{name} должен быть целым числом > 0, получено {value!r}")
    try:
        number = operator.index(value)  # type: ignore[arg-type]
    except TypeError:
        raise ValueError(
            f"{name} должен быть целым числом > 0, получено {value!r}"
        ) from None
    if number <= 0:
        raise ValueError(f"{name} должен быть положительным, получено {number}")
    return number


def _as_bool(value: object, name: str) -> bool:
    """Привести значение к ``bool``, понимая типичные ответы LLM.

    Принимаются ``bool``, целые ``0``/``1`` и строки ``"true"``/``"false"``
    (в том числе по-русски), чтобы ошибка типа не превращалась в неверный ответ.
    """
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().casefold()
        if text in _TRUE_STRINGS:
            return True
        if text in _FALSE_STRINGS:
            return False
    raise ValueError(f"{name} должен быть булевым значением, получено {value!r}")


def _period_alias(freq: str) -> str:
    """Привести пользовательскую частоту к алиасу, валидному для ``to_period``."""
    if not isinstance(freq, str) or not freq.strip():
        raise ValueError(f"freq должен быть непустой строкой, получено {freq!r}")
    alias = _PERIOD_ALIASES.get(freq.strip().upper())
    if alias is None:
        allowed = sorted(set(_PERIOD_ALIASES))
        raise ValueError(f"Неподдерживаемая частота freq={freq!r}. Допустимо: {allowed}")
    return alias


def _to_naive_day(value: pd.Timestamp) -> pd.Timestamp:
    """Убрать часовой пояс и время -- остаётся дата на начало дня."""
    if value.tz is not None:
        value = value.tz_localize(None)
    return value.normalize()


def _to_timestamp(value: DateLike) -> pd.Timestamp:
    """Преобразовать дату из параметров в ``Timestamp`` с обнулением времени.

    Принимает ISO-строку, ``ДД.ММ.ГГГГ``, ``date``/``datetime``, ``Timestamp``
    (в том числе с часовым поясом) и ``numpy.datetime64``.

    Raises
    ------
    TypeError
        Неподдерживаемый тип значения.
    ValueError
        Строка не распознана как дата.
    """
    if isinstance(value, pd.Timestamp):
        return _to_naive_day(value)
    if isinstance(value, datetime):
        return _to_naive_day(pd.Timestamp(value))
    if isinstance(value, date):
        return pd.Timestamp(value)
    if isinstance(value, np.datetime64):
        return _to_naive_day(pd.Timestamp(value))
    if isinstance(value, str):
        text = value.strip()
        for date_format in ("%Y-%m-%d", "%d.%m.%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return pd.Timestamp(datetime.strptime(text, date_format)).normalize()
            except ValueError:
                continue
        raise ValueError(
            f"Не удалось разобрать дату {value!r}. Ожидается 'ГГГГ-ММ-ДД' или 'ДД.ММ.ГГГГ'."
        )
    raise TypeError(f"Неподдерживаемый тип даты: {type(value)!r}")


def _as_period(period: Period, name: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Проверить и разобрать пару дат ``(начало, конец)``."""
    if isinstance(period, str):
        raise TypeError(
            f"{name} должен быть парой дат (начало, конец), получено {period!r}"
        )
    try:
        start_value, end_value = period  # type: ignore[misc]
    except (TypeError, ValueError):
        raise TypeError(
            f"{name} должен быть парой дат (начало, конец), получено {period!r}"
        ) from None
    start = _to_timestamp(start_value)
    end = _to_timestamp(end_value)
    if start > end:
        raise ValueError(
            f"{name}: начало периода {start.date()} позже конца {end.date()}"
        )
    return start, end


_NUMERIC_OUTPUT: Final[frozenset[str]] = frozenset(
    {DELTA, DELTA_PCT, SHARE_PCT, P1_VALUE, P2_VALUE, *METRICS}
)
"""Колонки результата, которые всегда должны быть числовыми."""

_INTEGER_OUTPUT: Final[frozenset[str]] = frozenset({RANK, ROWS_COUNT, UNDATED_ROWS})


def _empty_frame(columns: Sequence[str]) -> pd.DataFrame:
    """Пустой DataFrame с ожидаемыми колонками и типами.

    Пустой результат должен иметь тот же контракт, что и непустой (имена и типы
    колонок), иначе Streamlit и downstream-код получат ``object`` вместо чисел.
    """
    data: dict[str, pd.Series] = {}
    for column in columns:
        if column in _NUMERIC_OUTPUT:
            data[column] = pd.Series(dtype="float64")
        elif column in _INTEGER_OUTPUT:
            data[column] = pd.Series(dtype="int64")
        elif column == PERIOD:
            data[column] = pd.Series(dtype="datetime64[us]")
        else:
            data[column] = pd.Series(dtype="string")
    return pd.DataFrame(data)
