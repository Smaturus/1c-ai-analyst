"""Текстовая сводка и её структурированный payload — без Streamlit.

Модуль отвечает только за представление результата: те же строки, что раньше
строились в ``app.py``, и :class:`SummaryPayload` с числами для факт-чека
GoldSet (см. ADR 0013, D5). Никаких импортов Streamlit и pandas-отрисовки:
``build_summary`` остаётся обратно совместимой обёрткой, а числа для eval
берутся из ``payload.values``, а не парсятся из текста.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import pandas as pd

from onec_analyst.analytics import (
    COUNTERPARTY,
    DELTA,
    DIRECTION,
    PERIOD,
    PRODUCT,
    PRODUCT_GROUP,
    QUANTITY,
    REVENUE,
    ROWS_COUNT,
    STATUS,
)
from onec_analyst.entities import resolve_entity

#: Единица измерения выручки для подписей и текста сводки.
REVENUE_UNIT = "₽"
#: Единица измерения количества для подписей и текста сводки.
QUANTITY_UNIT = "шт."

#: Названия месяцев для человекочитаемых периодов в ``SummaryPayload``.
_MONTH_NAMES = (
    "январь",
    "февраль",
    "март",
    "апрель",
    "май",
    "июнь",
    "июль",
    "август",
    "сентябрь",
    "октябрь",
    "ноябрь",
    "декабрь",
)


@dataclass(frozen=True)
class SummaryPayload:
    """Структурированное представление сводки для eval и факт-чека.

    Attributes
    ----------
    text:
        Готовая строка сводки (байт-в-байт как раньше).
    intent:
        Имя интента, по которому построен результат.
    params:
        Параметры DSL, использованные для расчёта.
    values:
        Числовые значения: ``{"item", "metric", "period", "value"}``. Поле
        ``period`` — канонический ключ (``2023``, ``2024-Q1``, ``2024-03``),
        по нему факт-чек находит число в payload.
    periods:
        Человекочитаемые периоды из параметров: ``2023``, ``2024-Q1``,
        ``март 2024``, диапазон дат.
    entities:
        Сущности результата (товары, контрагенты, товарные группы).
    """

    text: str
    intent: str
    params: dict[str, Any]
    values: tuple[dict[str, Any], ...]
    periods: tuple[str, ...]
    entities: tuple[str, ...]


# --------------------------------------------------------------------------- #
# Форматирование чисел
# --------------------------------------------------------------------------- #
def _money(value: Any) -> str:
    """Отформатировать сумму в рублях: пробелы между разрядами, запятая в дробях."""
    try:
        return f"{float(value):,.2f}".replace(",", " ").replace(".", ",")
    except (TypeError, ValueError):
        return "—"


def format_percent(value: Any, digits: int = 2) -> str:
    """Отформатировать процент с запятой в качестве десятичного разделителя.

    Безопасна к ``None`` и ``NaN``: возвращает прочерк, а не исключение.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(number):
        return "—"
    return f"{number:.{digits}f}".replace(".", ",")


def format_quantity(value: Any) -> str:
    """Отформатировать количество: целое без «.00», если дробной части нет.

    Функция безопасна для «грязных» значений: ``None``, ``NaN``, ``inf`` и
    строки не роняют отрисовку страницы, а превращаются в прочерк.

    Examples
    --------
    >>> format_quantity(595)
    '595'
    >>> format_quantity(595.0)
    '595'
    >>> format_quantity(10_878)
    '10 878'
    >>> format_quantity(1.5)
    '1,5'
    >>> format_quantity(None)
    '—'
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(number):
        return "—"
    if number.is_integer():
        return f"{int(number):,}".replace(",", " ")
    return f"{number:.3f}".rstrip("0").rstrip(".").replace(".", ",")


def format_metric(value: Any, metric: str) -> str:
    """Отформатировать значение метрики с учётом её единиц измерения.

    ``revenue`` печатается как деньги (два знака), ``quantity`` — как
    количество (целое без хвостовых нулей).
    """
    return format_quantity(value) if metric == QUANTITY else _money(value)


def metric_unit(metric: str) -> str:
    """Вернуть единицу измерения для подписи метрики (``₽`` или ``шт.``)."""
    return REVENUE_UNIT if metric == REVENUE else QUANTITY_UNIT


def _period_value_label(period: Any, unit: str, fallback: str) -> str:
    """Заголовок колонки периода с датами: «01.01.2023–31.12.2023, ₽»."""
    try:
        start, end = period
        start_label = pd.Timestamp(start).strftime("%d.%m.%Y")
        end_label = pd.Timestamp(end).strftime("%d.%m.%Y")
    except (TypeError, ValueError):
        return fallback
    return f"{start_label}–{end_label}, {unit}"


def _status_label(value: Any) -> str:
    """Человекочитаемый статус L4L-сравнения с цветным маркером."""
    return {
        "new": "🔵 вошёл в топ",
        "lost": "🔴 выпал из топа",
        "up": "🟢 поднялся",
        "down": "🟠 опустился",
        "same": "⚪ на том же месте",
    }.get(str(value), str(value))


def _direction_label(value: str) -> str:
    """Человекочитаемая подпись направления изменения."""
    return {
        "up": "рост",
        "down": "падение",
        "flat": "без изменений",
        "new": "новый",
        "lost": "выпал",
    }.get(str(value), str(value))


def _human_date(value: Any) -> str:
    """ISO-дата (``2023-01-01``) в человеческий вид ``01.01.2023``."""
    try:
        return pd.Timestamp(value).strftime("%d.%m.%Y")
    except (TypeError, ValueError):
        return str(value)


def _filter_note(decision: Any, result: pd.DataFrame) -> str:
    """Пояснение к сводке, если запрос был сужен фильтром товара/группы."""
    params = decision.params or {}
    filter_text = params.get("product_filter") or params.get("group_filter")
    if not filter_text:
        return ""
    if isinstance(filter_text, str):
        column = PRODUCT if params.get("product_filter") else PRODUCT_GROUP
        if column in result.columns:
            catalog = result[column].dropna().astype("string").unique().tolist()
            filter_text = resolve_entity(filter_text, catalog).value
    return f"По фильтру «{filter_text}» найдено {len(result)} записей."


# --------------------------------------------------------------------------- #
# Периоды payload
# --------------------------------------------------------------------------- #
def _as_timestamp(value: Any) -> pd.Timestamp | None:
    """Привести дату/строку к ``Timestamp``; ``None`` для пустых значений."""
    if value is None:
        return None
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(stamp) else stamp


def _as_pair(value: Any) -> tuple[Any, Any] | None:
    """Вернуть пару дат, если значение похоже на ``period1``/``period2``."""
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return value[0], value[1]
    return None


def _month_end(stamp: pd.Timestamp) -> pd.Timestamp:
    """Последний день месяца для отметки ``stamp``."""
    return stamp + pd.offsets.MonthEnd(0)


def _single_period_label(stamp: pd.Timestamp, freq: str) -> str:
    """Канонический период одного интервала: ``2024``, ``2024-Q1``, ``2024-03``."""
    key = str(freq or "M").upper()
    if key in ("Y", "YE", "A", "AS", "YS"):
        return str(stamp.year)
    if key in ("Q", "QE", "QS"):
        return f"{stamp.year}-Q{stamp.quarter}"
    if key in ("M", "ME", "MS"):
        return f"{stamp.year}-{stamp.month:02d}"
    return stamp.strftime("%Y-%m-%d")


def _canonical_period(start: Any, end: Any) -> str:
    """Канонический ключ периода: год, квартал, месяц или диапазон дат."""
    start_ts = _as_timestamp(start)
    end_ts = _as_timestamp(end)
    if start_ts is not None and end_ts is not None:
        if (
            start_ts.year == end_ts.year
            and (start_ts.month, start_ts.day) == (1, 1)
            and (end_ts.month, end_ts.day) == (12, 31)
        ):
            return str(start_ts.year)
        quarter_start = start_ts.month in (1, 4, 7, 10) and start_ts.day == 1
        quarter_end = end_ts.month in (3, 6, 9, 12) and end_ts.day == _month_end(end_ts).day
        if (
            start_ts.year == end_ts.year
            and start_ts.quarter == end_ts.quarter
            and quarter_start
            and quarter_end
        ):
            return f"{start_ts.year}-Q{start_ts.quarter}"
        if (
            (start_ts.year, start_ts.month) == (end_ts.year, end_ts.month)
            and start_ts.day == 1
            and end_ts.day == _month_end(end_ts).day
        ):
            return f"{start_ts.year}-{start_ts.month:02d}"
        return (
            f"{start_ts.strftime('%Y-%m-%d')}–{end_ts.strftime('%Y-%m-%d')}"
        )
    if start_ts is not None:
        return start_ts.strftime("%Y-%m-%d")
    if end_ts is not None:
        return end_ts.strftime("%Y-%m-%d")
    return ""


def _human_period(canonical: str) -> str:
    """Человекочитаемая подпись канонического периода (для ``payload.periods``)."""
    if len(canonical) == 7 and canonical[4] == "-":
        year, month = canonical.split("-")
        if month.isdigit() and 1 <= int(month) <= 12:
            return f"{_MONTH_NAMES[int(month) - 1]} {year}"
    return canonical


def _periods_from_params(params: dict[str, Any]) -> tuple[str, ...]:
    """Человекочитаемые периоды из параметров DSL (без изменения текста)."""
    periods: list[str] = []
    for key in ("period1", "period2"):
        pair = _as_pair(params.get(key))
        if pair is not None:
            label = _human_period(_canonical_period(*pair))
            if label and label not in periods:
                periods.append(label)
    if periods:
        return tuple(periods)
    start_date = params.get("start_date")
    end_date = params.get("end_date")
    if start_date is None or end_date is None:
        return ()
    label = _human_period(_canonical_period(start_date, end_date))
    return (label,) if label else ()


#: Римские номера кварталов для человекочитаемого периода в сводке.
_ROMAN_QUARTERS = ("I", "II", "III", "IV")


def _period_phrase(label: str, *, short: bool = False) -> str:
    """Подпись периода для текста: «2023 год», «I квартал 2024», «март 2024».

    ``short`` — для пары периодов: «2023 → 2024» вместо «2023 год → 2024 год».
    """
    text = str(label or "").strip()
    if not text:
        return ""
    if len(text) == 4 and text.isdigit():
        return text if short else f"{text} год"
    if len(text) == 7 and text[4] == "-" and text[5] == "Q" and text[6] in "1234":
        if short:
            return text
        return f"{_ROMAN_QUARTERS[int(text[6]) - 1]} квартал {text[:4]}"
    if "–" in text:
        if short:
            return text
        start, end = text.split("–", 1)
        return f"период с {_human_date(start)} по {_human_date(end)}"
    return text


def _summary_period_prefix(decision: Any) -> str:
    """Префикс сводки с периодом: «За 2023 год» или «2023 → 2024»."""
    periods = _periods_from_params(decision.params or {})
    if not periods:
        return ""
    if len(periods) > 1:
        return (
            f"{_period_phrase(periods[0], short=True)} → "
            f"{_period_phrase(periods[1], short=True)}"
        )
    return f"За {_period_phrase(periods[0])}"


# --------------------------------------------------------------------------- #
# Сущности результата
# --------------------------------------------------------------------------- #
def extract_entities(decision: Any, result: pd.DataFrame | None) -> list[str]:
    """Сущности результата: товары, контрагенты или товарные группы.

    Используется и ``SummaryPayload.entities``, и
    :func:`onec_analyst.pipeline.extract_entities`, чтобы правило было одно.
    """
    if result is None or result.empty:
        return []
    intent = decision.intent or ""
    params = decision.params or {}
    if intent in ("top_bottom_products", "compare_top_products"):
        column = PRODUCT
    elif intent == "negative_revenue":
        column = str(params.get("group_by", PRODUCT))
    elif intent == "counterparty_dynamics":
        column = COUNTERPARTY
    elif intent == "group_dynamics":
        column = (
            PRODUCT_GROUP if PRODUCT_GROUP in result.columns else result.columns[0]
        )
    else:
        return []
    if column not in result.columns:
        return []

    entities: list[str] = []
    for value in result[column].tolist():
        if value is None:
            continue
        try:
            if pd.isna(value):
                continue
        except (TypeError, ValueError):
            pass
        text = str(value)
        if text not in entities:
            entities.append(text)
    return entities


# --------------------------------------------------------------------------- #
# Числа payload
# --------------------------------------------------------------------------- #
def _value_row(
    item: Any,
    metric: str,
    period: str,
    value: Any,
) -> dict[str, Any] | None:
    """Собрать одну строку ``values``; нечисловые/пустые значения пропускаются."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return {
        "item": str(item),
        "metric": str(metric),
        "period": str(period),
        "value": number,
    }


def _payload_values(
    decision: Any,
    result: pd.DataFrame,
) -> tuple[dict[str, Any], ...]:
    """Числа для факт-чека по 7 интентам (минимум, заданный ADR 0013)."""
    intent = decision.intent or ""
    params = decision.params or {}
    metric = str(params.get("metric", REVENUE))
    period = _canonical_period(params.get("start_date"), params.get("end_date"))

    rows: list[dict[str, Any] | None] = []

    if intent == "total":
        row = result.iloc[0]
        rows.append(_value_row("Итого", REVENUE, period, row.get(REVENUE)))
        rows.append(_value_row("Итого", QUANTITY, period, row.get(QUANTITY)))
    elif intent == "top_bottom_products":
        if PRODUCT in result.columns and metric in result.columns:
            row = result.iloc[0]
            rows.append(_value_row(row[PRODUCT], metric, period, row.get(metric)))
    elif intent == "timeseries":
        if metric in result.columns:
            rows.append(
                _value_row("Сумма", metric, period, float(result[metric].sum()))
            )
            if PERIOD in result.columns and not result[metric].dropna().empty:
                peak = result.loc[result[metric].idxmax()]
                peak_label = _single_period_label(
                    pd.Timestamp(peak[PERIOD]), str(params.get("freq", "M"))
                )
                rows.append(_value_row("Пик", metric, peak_label, peak[metric]))
    elif intent == "negative_revenue":
        if REVENUE in result.columns:
            rows.append(
                _value_row("Возвраты", REVENUE, period, float(result[REVENUE].sum()))
            )
    elif intent in ("counterparty_dynamics", "group_dynamics"):
        if DELTA in result.columns and not result.empty:
            dim = result.columns[0]
            pair = _as_pair(params.get("period2"))
            p2 = _canonical_period(*pair) if pair is not None else period
            best = result.sort_values(DELTA, ascending=False).iloc[0]
            worst = result.sort_values(DELTA).iloc[0]
            rows.append(_value_row(best[dim], "delta", p2, best[DELTA]))
            if worst.name != best.name:
                rows.append(_value_row(worst[dim], "delta", p2, worst[DELTA]))
    elif intent == "compare_top_products":
        value_p2 = f"{metric}_p2"
        pair = _as_pair(params.get("period2"))
        p2 = _canonical_period(*pair) if pair is not None else period
        if PRODUCT in result.columns and value_p2 in result.columns:
            for _, row in result.iterrows():
                rows.append(_value_row(row[PRODUCT], metric, p2, row[value_p2]))

    return tuple(row for row in rows if row is not None)


# --------------------------------------------------------------------------- #
# Текст сводки
# --------------------------------------------------------------------------- #
def _summary_text(
    decision: Any,
    result: pd.DataFrame | None,
    data_profile: dict[str, Any] | None = None,
) -> str:
    """Собрать текст сводки с периодом (этап 1.5, требование факт-чека)."""
    params = decision.params or {}
    filter_text = params.get("product_filter") or params.get("group_filter")
    metric = str(params.get("metric", REVENUE))
    metric_label = "выручке" if metric == REVENUE else "количеству"
    period_prefix = _summary_period_prefix(decision)
    periods = _periods_from_params(params)
    total_zero = (
        decision.intent == "total"
        and result is not None
        and not result.empty
        and ROWS_COUNT in result.columns
        and int(result.iloc[0][ROWS_COUNT]) == 0
    )
    if result is None or result.empty or total_zero:
        if total_zero and filter_text:
            if periods:
                phrase = _period_phrase(periods[0])
                return (
                    f"По фильтру «{filter_text}» за {phrase} "
                    f"данных по {metric_label} не найдено."
                )
            return f"По фильтру «{filter_text}» данных не найдено за выбранный период."
        profile = data_profile or {}
        date_min = profile.get("date_min")
        date_max = profile.get("date_max")
        if date_min and date_max:
            base = (
                f"данных по {metric_label} нет. "
                f"В текущем источнике данные доступны с {_human_date(date_min)} "
                f"по {_human_date(date_max)}."
            )
        else:
            base = (
                f"данных по {metric_label} нет — "
                "попробуйте другой период или источник."
            )
        if period_prefix:
            return f"{period_prefix}: {base}"
        fallback = (
            "За выбранный период данных нет. "
            f"В текущем источнике данные доступны с {_human_date(date_min)} "
            f"по {_human_date(date_max)}."
            if date_min and date_max
            else "За выбранный период данных нет — попробуйте другой период или источник."
        )
        note = "" if total_zero else _filter_note(decision, result)
        return f"{note} {fallback}".strip() if note else fallback
    summary = _build_summary_text(decision, result)
    if period_prefix:
        summary = f"{period_prefix}: {summary}"
    note = _filter_note(decision, result)
    return f"{note} {summary}" if note else summary


def _build_summary_text(decision: Any, result: pd.DataFrame) -> str:
    """Текст сводки для непустого результата."""
    intent = decision.intent or ""
    params = decision.params or {}
    metric = str(params.get("metric", REVENUE))
    metric_label = "выручке" if metric == REVENUE else "количеству"
    metric_noun = "выручка" if metric == REVENUE else "количество"
    unit = metric_unit(metric)

    if intent == "top_bottom_products":
        value_column = metric
        top_row = result.iloc[0]
        share = (
            f" ({format_percent(top_row['share_pct'])} % от суммы)"
            if "share_pct" in result.columns and pd.notna(top_row.get("share_pct"))
            else ""
        )
        word = "Худший" if bool(params.get("ascending")) else "Лидер"
        return (
            f"{word}: «{top_row[PRODUCT]}» — {format_metric(top_row[value_column], metric)} {unit}{share}. "
            f"В выборке {len(result)} товар(ов) по {metric_label}."
        )
    if intent == "timeseries":
        value_column = metric
        total = float(result[value_column].sum())
        first, last = result.iloc[0], result.iloc[-1]
        peak = result.loc[result[value_column].idxmax()]
        first_label = pd.Timestamp(first[PERIOD]).strftime("%Y-%m")
        last_label = pd.Timestamp(last[PERIOD]).strftime("%Y-%m")
        peak_label = pd.Timestamp(peak[PERIOD]).strftime("%Y-%m")
        return (
            f"{len(result)} интервал(ов) по {metric_label}, суммарно {format_metric(total, metric)} {unit}. "
            f"В первом ({first_label}): {format_metric(first[value_column], metric)} {unit}, "
            f"в последнем ({last_label}): {format_metric(last[value_column], metric)} {unit}, "
            f"максимум: {format_metric(peak[value_column], metric)} {unit} "
            f"({peak_label})."
        )
    if intent in ("counterparty_dynamics", "group_dynamics"):
        dim = result.columns[0]
        up = int((result[DIRECTION].astype(str) == "up").sum()) if DIRECTION in result.columns else int(result[DELTA].gt(0).sum())
        down = int((result[DIRECTION].astype(str) == "down").sum()) if DIRECTION in result.columns else int(result[DELTA].lt(0).sum())
        new = int((result[DIRECTION].astype(str) == "new").sum()) if DIRECTION in result.columns else 0
        lost = int((result[DIRECTION].astype(str) == "lost").sum()) if DIRECTION in result.columns else 0
        if bool(params.get("only_drops")):
            least = result.sort_values(DELTA, ascending=False).iloc[0]
            biggest = result.sort_values(DELTA).iloc[0]
            return (
                f"Показаны только падения: {len(result)} значени(й), падение — {down}, "
                f"выпало — {lost}. "
                f"Наименьшее падение: «{least[dim]}» ({format_metric(least[DELTA], metric)} {unit}); "
                f"наибольшее падение: «{biggest[dim]}» ({format_metric(biggest[DELTA], metric)} {unit}). "
                f"Показатель — {metric_noun}."
            )
        # «Рост» — только строки с положительной дельтой и ненулевой базой:
        # рост с нуля («новый») несравним с ростом от ненулевой базы и
        # выносится отдельной строкой (DECISIONS: zero-base не ранжируется как рост).
        has_direction = DIRECTION in result.columns
        is_new = result[DIRECTION].astype(str) == "new" if has_direction else False
        growth = result.loc[result[DELTA].gt(0) & ~is_new]
        is_lost = result[DIRECTION].astype(str) == "lost" if has_direction else False
        decline = result.loc[result[DELTA].lt(0) & ~is_lost]
        if not growth.empty:
            best = growth.sort_values(DELTA, ascending=False).iloc[0]
            growth_clause = (
                f"Наибольший рост: «{best[dim]}» "
                f"({format_metric(best[DELTA], metric)} {unit}, "
                f"{_direction_label(best.get(DIRECTION, ''))}); "
            )
        else:
            growth_clause = "Роста нет: все значения снизились или выпали; "
        newcomers_clause = ""
        if has_direction and new:
            newcomers = result.loc[is_new]
            if not newcomers.empty:
                top_new = newcomers.sort_values(DELTA, ascending=False).iloc[0]
                newcomers_clause = (
                    f"Новый участник: «{top_new[dim]}» — с нуля до "
                    f"{format_metric(top_new[DELTA], metric)} {unit}. "
                )
        lost_clause = ""
        if has_direction and lost:
            losers = result.loc[is_lost]
            if not losers.empty:
                top_lost = losers.sort_values(DELTA).iloc[0]
                lost_clause = (
                    f"«{top_lost[dim]}» отсутствует в периоде 2 "
                    f"(в периоде 1 — {format_metric(-top_lost[DELTA], metric)} {unit}). "
                )
        if not decline.empty:
            worst = decline.sort_values(DELTA).iloc[0]
            decline_clause = (
                f"наибольшее падение: «{worst[dim]}» "
                f"({format_metric(worst[DELTA], metric)} {unit}). "
            )
        else:
            decline_clause = "падений нет. "
        return (
            f"Сравнение по {len(result)} значени(ям): рост — {up}, падение — {down}, "
            f"новых — {new}, выпало — {lost}. "
            f"{growth_clause}{newcomers_clause}{lost_clause}{decline_clause}"
            f"Показатель — {metric_noun}."
        )
    if intent == "total":
        row = result.iloc[0]
        rows = int(row[ROWS_COUNT]) if ROWS_COUNT in result.columns else len(result)
        return (
            f"Выручка без НДС: {_money(row[REVENUE])} {REVENUE_UNIT}; "
            f"количество: {format_quantity(row[QUANTITY])} {QUANTITY_UNIT}; "
            f"строк в выборке: {rows}; строк без даты: {int(row.get('undated_rows', 0))}."
        )
    if intent == "compare_top_products":
        return _compare_tops_summary(result, params, metric, unit)
    if intent == "negative_revenue":
        dim = str(params.get("group_by", PRODUCT))
        noun = "контрагентов" if dim == COUNTERPARTY else "товаров"
        total = float(result[REVENUE].sum())
        rows = int(result[ROWS_COUNT].sum()) if ROWS_COUNT in result.columns else len(result)
        return (
            f"Найдено {len(result)} {noun} с отрицательной выручкой. "
            f"Общая сумма возвратов: {format_metric(total, REVENUE)} {REVENUE_UNIT} "
            f"({rows} строк)."
        )
    return f"Получено строк: {len(result)}."


def _compare_tops_summary(
    result: pd.DataFrame,
    params: dict[str, Any],
    metric: str,
    unit: str,
) -> str:
    """Сводка L4L-сравнения: сколько удержали/вошли/выпали и самый заметный рост."""
    top_n = int(params.get("top_n", 5))
    statuses = result[STATUS].astype(str) if STATUS in result.columns else pd.Series(dtype=str)
    counts = statuses.value_counts()
    same = int(counts.get("same", 0))
    up = int(counts.get("up", 0))
    down = int(counts.get("down", 0))
    new = int(counts.get("new", 0))
    lost = int(counts.get("lost", 0))
    parts = [
        f"Сравнение топ-{top_n}: удержали место — {same}, поднялись — {up}, опустились — {down}, "
        f"вошли в топ — {new}, выпали — {lost}."
    ]
    value_p1 = f"{metric}_p1"
    value_p2 = f"{metric}_p2"
    if DELTA in result.columns and result[DELTA].notna().any():
        growth = result.loc[result[DELTA].idxmax()]
        parts.append(
            f"Наибольший рост: «{growth[PRODUCT]}» (+{format_metric(growth[DELTA], metric)} {unit}"
            f" — {format_metric(growth[value_p1], metric)} → {format_metric(growth[value_p2], metric)} {unit})."
        )
    if new:
        newcomer = result.loc[result[STATUS].astype(str) == "new"].iloc[0]
        parts.append(
            f"Новичок топа: «{newcomer[PRODUCT]}» — {format_metric(newcomer[value_p2], metric)} {unit}."
        )
    if lost:
        outsider = result.loc[result[STATUS].astype(str) == "lost"].iloc[0]
        parts.append(
            f"Выпал из топа: «{outsider[PRODUCT]}» (было {format_metric(outsider[value_p1], metric)} {unit})."
        )
    metric_noun = "выручка" if metric == REVENUE else "количество"
    parts.append(f"Показатель — {metric_noun}.")
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# Публичные функции
# --------------------------------------------------------------------------- #
def build_summary_payload(
    decision: Any,
    result: pd.DataFrame | None,
    data_profile: dict[str, Any] | None = None,
) -> SummaryPayload:
    """Собрать текст сводки и структурированный payload для eval."""
    params = dict(decision.params or {})
    intent = decision.intent or ""
    text = _summary_text(decision, result, data_profile)
    empty = result is None or result.empty
    if result is not None and not empty:
        total_zero = (
            intent == "total"
            and ROWS_COUNT in result.columns
            and int(result.iloc[0][ROWS_COUNT]) == 0
        )
        empty = total_zero
    values = () if empty else _payload_values(decision, result)
    entities = () if empty else tuple(extract_entities(decision, result))
    return SummaryPayload(
        text=text,
        intent=intent,
        params=params,
        values=values,
        periods=_periods_from_params(params),
        entities=entities,
    )


def build_summary(
    decision: Any,
    result: pd.DataFrame | None,
    data_profile: dict[str, Any] | None = None,
) -> str:
    """Собрать текстовую сводку по результату (без участия LLM).

    Обратно совместимая обёртка: текст — это ``SummaryPayload.text``.
    """
    return build_summary_payload(decision, result, data_profile).text
