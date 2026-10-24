"""Streamlit-приложение «ИИ-ассистент для анализа данных из 1С».

Поток: вопрос пользователя → LLM (или офлайн-правила) → JSON-DSL →
валидация Pydantic → :func:`analytics.dispatch_intent` → pandas → таблица,
график, текстовая сводка и блок «Технические детали» (JSON-DSL).

Запуск::

    streamlit run app.py

Переменные окружения для LLM (опционально)::

    LLM_PROVIDER=deepseek|openai|ollama|none
    LLM_API_KEY=...
    LLM_MODEL=deepseek-chat
    LLM_BASE_URL=https://api.deepseek.com/v1

Без ``LLM_PROVIDER`` приложение работает в офлайн-режиме на детерминированном
разборе типовых вопросов — удобно для демо и приёмки без ключа API.
"""

from __future__ import annotations

import math
import tempfile
from pathlib import Path
from typing import Any

import altair as alt
import pandas as pd
import streamlit as st

from analytics import (
    CHECK,
    COUNTERPARTY,
    DATE,
    DELTA,
    DIRECTION,
    INTENT_REGISTRY,
    PERIOD,
    PRODUCT,
    QUANTITY,
    REVENUE,
    ROWS_COUNT,
    VALUE,
    dispatch_intent,
    get_data_quality,
)
from data_loader import load_and_clean_1c_report
from llm_agent import (
    INTENT_EXAMPLES,
    INTENT_NAMES,
    AgentDecision,
    ClarificationPlan,
    LLMPlanner,
    LLMSettings,
    RuleBasedPlanner,
    check_schema_sync,
    settings_from_env,
)
from onec_source import load_sample_vitrina, load_vitrina

APP_TITLE = "ИИ-ассистент анализа данных 1С"

REVENUE_UNIT = "₽"
QUANTITY_UNIT = "шт."

SOURCE_SAMPLE = "Демо-данные: sample_data.csv"
SOURCE_UPLOAD = "Загрузить свой файл"

DATASET_SAMPLE = "sample_data.csv"

INTENT_TITLES: dict[str, str] = {
    "top_bottom_products": "Топ/антитоп товаров",
    "timeseries": "Временной ряд продаж",
    "counterparty_dynamics": "Динамика контрагентов",
    "group_dynamics": "Срез по товарным группам",
    "total": "Итог за период",
}


# --------------------------------------------------------------------------- #
# Загрузка данных
# --------------------------------------------------------------------------- #
@st.cache_data(show_spinner=False)
def load_dataset_sample() -> pd.DataFrame:
    """Загрузить синтетические демо-данные (``sample_data.csv``, без реальных выгрузок)."""
    return load_sample_vitrina(DATASET_SAMPLE)


@st.cache_data(show_spinner=False)
def load_uploaded(name: str, payload: bytes) -> pd.DataFrame:
    """Загрузить файл пользователя (CSV 1С или Excel) через временный файл."""
    suffix = Path(name).suffix.lower()
    if suffix not in (".csv", ".xlsx", ".xls"):
        raise ValueError(f"Неподдерживаемый формат файла: {suffix}. Ожидается .csv или .xlsx")
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        handle.write(payload)
        handle.close()
        if suffix == ".csv":
            # Поддерживаем и 6-колоночную демо-схему, и полную выгрузку 1С (11 колонок).
            header = pd.read_csv(handle.name, nrows=0)
            if "revenue_net" not in header.columns:
                return load_sample_vitrina(handle.name)
            return load_vitrina(handle.name)
        return load_and_clean_1c_report(handle.name)
    finally:
        Path(handle.name).unlink(missing_ok=True)


def load_dataframe(kind: str, uploaded: Any) -> tuple[pd.DataFrame, str]:
    """Выбрать и загрузить витрину по выбору в боковой панели."""
    if kind == SOURCE_SAMPLE:
        return load_dataset_sample(), "демо-данные"
    if uploaded is None:
        raise ValueError("Файл не загружен")
    return load_uploaded(uploaded.name, uploaded.getvalue()), uploaded.name


def data_profile(df: pd.DataFrame, source: str) -> dict[str, Any]:
    """Профиль данных для системного промпта LLM (без тяжёлых вычислений)."""
    dates = df[DATE].dropna() if DATE in df.columns else pd.Series(dtype="datetime64[ns]")
    return {
        "rows": int(len(df)),
        "date_min": dates.min().date().isoformat() if not dates.empty else None,
        "date_max": dates.max().date().isoformat() if not dates.empty else None,
        "products": int(df[PRODUCT].nunique()) if PRODUCT in df.columns else None,
        "counterparties": int(df[COUNTERPARTY].nunique()) if COUNTERPARTY in df.columns else None,
        "source": source,
    }


# --------------------------------------------------------------------------- #
# Форматирование и текстовая сводка
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
    '1.5'
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


COLUMN_LABELS: dict[str, str] = {
    "rank": "№",
    "product": "Товар",
    "product_group": "Товарная группа",
    "counterparty": "Контрагент",
    "period": "Период",
    "period_start": "Начало периода",
    "period_end": "Конец периода",
    "share_pct": "Доля, %",
    "delta_pct": "Изменение, %",
    "direction": "Направление",
    "rows_count": "Строк в выборке",
    "undated_rows": "Строк без даты",
}
# Русские подписи колонок для таблицы результатов (JSON-DSL виден только в
# «Технических деталях»). Это комментарий, а не строковый литерал: Streamlit
# magic рендерит «голые» строки уровня модуля как вывод на странице.


def _period_column_format(freq: str) -> str:
    """Формат даты для столбца периода: «MM.YYYY», «Q YYYY», «YYYY» или дата."""
    if freq in ("Y", "YE", "A", "AS", "YS"):
        return "YYYY"
    if freq in ("Q", "QE", "QS"):
        return "Q YYYY"
    if freq in ("M", "ME", "MS"):
        return "MM.YYYY"
    return "DD.MM.YYYY"


def _period_value_label(period: Any, unit: str, fallback: str) -> str:
    """Заголовок колонки периода с датами: «01.01.2023–31.12.2023, ₽»."""
    try:
        start, end = period
        start_label = pd.Timestamp(start).strftime("%d.%m.%Y")
        end_label = pd.Timestamp(end).strftime("%d.%m.%Y")
    except (TypeError, ValueError):
        return fallback
    return f"{start_label}–{end_label}, {unit}"


def _number_column(label: str, *, step: float | None = None) -> Any:
    """Числовой столбец с русской подписью и локализованным форматом.

    ``step`` задаёт число знаков после запятой (``0.01`` — два знака, ``1`` —
    целые). Значения остаются числами, поэтому клик по заголовку сортирует
    столбец, а не сортирует строки лексикографически.
    """
    options: dict[str, Any] = {"format": "localized"}
    if step is not None:
        options["step"] = step
    return st.column_config.NumberColumn(label, **options)


def _column_labels(decision: AgentDecision) -> dict[str, str]:
    """Русские подписи столбцов результата (JSON-DSL виден только в деталях)."""
    params = decision.params or {}
    metric = str(params.get("metric", REVENUE))
    unit = metric_unit(metric)
    labels = dict(COLUMN_LABELS)
    labels[REVENUE] = f"Выручка без НДС, {REVENUE_UNIT}"
    labels[QUANTITY] = f"Количество, {QUANTITY_UNIT}"
    if "period1" in params:
        labels["period1_value"] = _period_value_label(
            params["period1"], unit, f"Период 1, {unit}"
        )
    if "period2" in params:
        labels["period2_value"] = _period_value_label(
            params["period2"], unit, f"Период 2, {unit}"
        )
    labels["delta"] = f"Изменение, {unit}"
    return labels


def build_result_table(decision: AgentDecision, result: pd.DataFrame) -> pd.DataFrame:
    """Подготовить данные для таблицы, сохранив числа и даты сортируемыми.

    Отличие от «красивой строковой» таблицы: значения остаются числами и
    датами, поэтому ``st.dataframe`` умеет сортировать их по клику на заголовок.
    Проценты лишь округляются до двух знаков, чтобы локализованный формат не
    показывал длинные хвосты.
    """
    if result is None or result.empty:
        return result if result is not None else pd.DataFrame()
    display = result.copy()
    for column in ("share_pct", "delta_pct"):
        if column in display.columns:
            display[column] = display[column].astype(float).round(2)
    if DIRECTION in display.columns:
        # Слова «рост/падение» вместо технических up/down; сортировка по тексту остаётся.
        display[DIRECTION] = display[DIRECTION].map(_direction_label)
    return display


def build_column_config(decision: AgentDecision, result: pd.DataFrame) -> dict[str, Any]:
    """Настроить столбцы таблицы: русские заголовки и форматы без потери сортировки.

    Числа форматируются локализованно (пробелы в разрядах, запятая в дробях) с
    фиксированным числом знаков, даты — как ``ДД.ММ.ГГГГ``. Технические имена
    полей ``period``/``revenue``/``delta`` пользователь не видит.
    """
    if result is None or result.empty:
        return {}
    params = decision.params or {}
    metric = str(params.get("metric", REVENUE))
    freq = str(params.get("freq", "M"))
    labels = _column_labels(decision)
    money_step = 0.01 if metric == REVENUE else None
    config: dict[str, Any] = {}
    for column in result.columns:
        label = labels.get(column, column)
        if column == PERIOD:
            config[column] = st.column_config.DateColumn(
                label, format=_period_column_format(freq)
            )
        elif column in ("period_start", "period_end"):
            config[column] = st.column_config.DateColumn(label, format="DD.MM.YYYY")
        elif column == REVENUE or column in ("period1_value", "period2_value", DELTA):
            config[column] = _number_column(label, step=money_step)
        elif column == QUANTITY:
            config[column] = _number_column(label)
        elif column in ("share_pct", "delta_pct"):
            config[column] = _number_column(label, step=0.01)
        elif column in ("rank", ROWS_COUNT, "undated_rows"):
            config[column] = _number_column(label, step=1)
        else:
            config[column] = st.column_config.TextColumn(label)
    return config


def _direction_label(value: str) -> str:
    """Человекочитаемая подпись направления изменения."""
    return {
        "up": "рост",
        "down": "падение",
        "flat": "без изменений",
        "new": "новый",
        "lost": "выпал",
    }.get(str(value), str(value))


def build_summary(decision: AgentDecision, result: pd.DataFrame) -> str:
    """Собрать текстовую сводку по результату (без участия LLM)."""
    if result is None or result.empty:
        return "За выбранный период данных нет — попробуйте другой период или источник."
    intent = decision.intent or ""
    params = decision.params or {}
    metric = str(params.get("metric", REVENUE))
    metric_label = "выручке" if metric == REVENUE else "количеству"
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
        return (
            f"За период {len(result)} интервал(ов), суммарно {format_metric(total, metric)} {unit}. "
            f"Первый интервал: {format_metric(first[value_column], metric)} {unit}, "
            f"последний: {format_metric(last[value_column], metric)} {unit}, "
            f"максимум: {format_metric(peak[value_column], metric)} {unit} "
            f"({pd.Timestamp(peak[PERIOD]).strftime('%Y-%m')})."
        )
    if intent in ("counterparty_dynamics", "group_dynamics"):
        dim = result.columns[0]
        up = int((result[DIRECTION].astype(str) == "up").sum()) if DIRECTION in result.columns else int(result[DELTA].gt(0).sum())
        down = int((result[DIRECTION].astype(str) == "down").sum()) if DIRECTION in result.columns else int(result[DELTA].lt(0).sum())
        new = int((result[DIRECTION].astype(str) == "new").sum()) if DIRECTION in result.columns else 0
        lost = int((result[DIRECTION].astype(str) == "lost").sum()) if DIRECTION in result.columns else 0
        best = result.sort_values(DELTA, ascending=False).iloc[0]
        worst = result.sort_values(DELTA).iloc[0]
        best_label = _direction_label(best.get(DIRECTION, ""))
        if bool(params.get("only_drops")):
            return (
                f"Показаны только падения: {len(result)} значени(й), падение — {down}, "
                f"выпало — {lost}. "
                f"Наименьшее падение: «{best[dim]}» ({format_metric(best[DELTA], metric)} {unit}); "
                f"наибольшее падение: «{worst[dim]}» ({format_metric(worst[DELTA], metric)} {unit})."
            )
        return (
            f"Сравнение по {len(result)} значени(ям): рост — {up}, падение — {down}, новых — {new}, выпало — {lost}. "
            f"Наибольший рост: «{best[dim]}» ({format_metric(best[DELTA], metric)} {unit}, {best_label}); "
            f"наибольшее падение: «{worst[dim]}» ({format_metric(worst[DELTA], metric)} {unit})."
        )
    if intent == "total":
        row = result.iloc[0]
        rows = int(row[ROWS_COUNT]) if ROWS_COUNT in result.columns else len(result)
        return (
            f"Выручка без НДС: {_money(row[REVENUE])} {REVENUE_UNIT}; "
            f"количество: {format_quantity(row[QUANTITY])} {QUANTITY_UNIT}; "
            f"строк в выборке: {rows}; строк без даты: {int(row.get('undated_rows', 0))}."
        )
    return f"Получено строк: {len(result)}."


# --------------------------------------------------------------------------- #
# Отрисовка
# --------------------------------------------------------------------------- #
def _format_quality_value(value: Any) -> str:
    """Показать значение диагностики текстом: числа по-русски, даты как есть.

    Колонка «Значение» в таблице качества смешанная (числа и даты), поэтому
    приводим её к строке — иначе Arrow-сериализация в Streamlit ругается.
    """
    if value is None:
        return "—"
    try:
        if pd.isna(value):
            return "—"
    except (TypeError, ValueError):
        pass
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, int):
        return f"{value:,}".replace(",", " ")
    if isinstance(value, float):
        return f"{value:,.2f}".replace(",", " ").replace(".", ",")
    return str(value)


def render_dataset_quality(df: pd.DataFrame) -> None:
    """Показать качество данных в боковой панели."""
    st.sidebar.subheader("Качество данных")
    quality = get_data_quality(df)
    quality[VALUE] = quality[VALUE].map(_format_quality_value)
    quality = quality.rename(columns={CHECK: "Показатель", VALUE: "Значение"})
    st.sidebar.dataframe(quality, hide_index=True)
    st.sidebar.caption(
        "Строки без даты не участвуют в аналитике по периодам. "
        "Возвраты и сторно учтены со знаком «минус»."
    )


# --------------------------------------------------------------------------- #
# Графики (Altair, без новых зависимостей: altair приходит со Streamlit)
# --------------------------------------------------------------------------- #
CHART_HEIGHT = 420
# Базовая высота графика, px. (Комментарий, а не строковый литерал: Streamlit
# magic рендерит «голые» строки уровня модуля как вывод на странице.)

DIRECTION_COLORS: dict[str, str] = {
    "рост": "#2e7d32",
    "падение": "#c62828",
    "без изменений": "#9e9e9e",
    "новый": "#1565c0",
    "выпал": "#ef6c00",
}
# Цвета баров динамики по направлению изменения (для читаемости на графике).


def _chart_height(rows: int) -> int:
    """Высота горизонтального графика под число категорий (px)."""
    return min(600, max(240, 26 * rows))


def build_chart(decision: AgentDecision, result: pd.DataFrame) -> alt.Chart | None:
    """Собрать Altair-график для результата интента.

    Чистая функция без обращений к Streamlit: спецификацию удобно проверять
    через ``chart.to_dict()`` (поля, сортировка, заголовки осей), не поднимая
    UI. Возвращает ``None``, если график для интента не предусмотрен
    (``total`` рисуется metric-карточками) или в данных нет нужных колонок.

    Правила отрисовки:

    * топ/антитоп товаров — горизонтальные бары: ``y`` — товар, ``x`` — метрика,
      ``sort="-x"`` — лидер сверху, длинные названия не обрезаются;
    * динамика контрагентов/групп — горизонтальные бары по ``delta`` с
      сортировкой по значению; при наличии ``direction`` бары красятся по
      направлению (рост/падение/…);
    * временной ряд — линия: ``x`` — период, ``y`` — метрика;
    * подписи осей — русские, из :func:`_column_labels` (единицы ₽/шт.),
      названия колонок в данные не попадают.
    """
    if result is None or result.empty:
        return None

    intent = decision.intent or ""
    params = decision.params or {}
    metric = str(params.get("metric", REVENUE))
    labels = _column_labels(decision)
    metric_title = labels.get(metric, metric)

    if intent == "top_bottom_products":
        if PRODUCT not in result.columns or metric not in result.columns:
            return None
        return (
            alt.Chart(result)
            .mark_bar()
            .encode(
                x=alt.X(metric, type="quantitative", title=metric_title),
                y=alt.Y(
                    PRODUCT,
                    type="nominal",
                    title=labels.get(PRODUCT, PRODUCT),
                    sort="-x",
                ),
            )
            .properties(height=_chart_height(len(result)))
        )

    if intent in ("counterparty_dynamics", "group_dynamics"):
        dim = result.columns[0]
        if dim not in result.columns or DELTA not in result.columns:
            return None
        data = result.copy()
        if DIRECTION in data.columns:
            # Русские подписи в легенде; сортировка/значения не меняются.
            data[DIRECTION] = data[DIRECTION].map(_direction_label)
        chart = (
            alt.Chart(data)
            .mark_bar()
            .encode(
                x=alt.X(DELTA, type="quantitative", title=labels.get(DELTA, DELTA)),
                y=alt.Y(dim, type="nominal", title=labels.get(dim, dim), sort="-x"),
            )
        )
        if DIRECTION in data.columns:
            chart = chart.encode(
                color=alt.Color(
                    DIRECTION,
                    type="nominal",
                    title="Направление",
                    scale=alt.Scale(
                        domain=list(DIRECTION_COLORS),
                        range=list(DIRECTION_COLORS.values()),
                    ),
                )
            )
        return chart.properties(height=_chart_height(len(result)))

    if intent == "timeseries":
        if PERIOD not in result.columns or metric not in result.columns:
            return None
        return (
            alt.Chart(result)
            .mark_line(point=True)
            .encode(
                x=alt.X(PERIOD, type="temporal", title=labels.get(PERIOD, PERIOD)),
                y=alt.Y(metric, type="quantitative", title=metric_title),
            )
            .properties(height=CHART_HEIGHT)
        )

    return None


def render_chart(decision: AgentDecision, result: pd.DataFrame) -> None:
    """Показать график: Altair с русскими осями; для итога — metric-карточки."""
    if result is None or result.empty:
        return
    if decision.intent == "total":
        try:
            row = result.iloc[0]
            col1, col2, col3 = st.columns(3)
            col1.metric("Выручка без НДС, ₽", _money(row[REVENUE]))
            col2.metric("Количество", format_quantity(row[QUANTITY]))
            col3.metric("Строк в выборке", int(row.get(ROWS_COUNT, len(result))))
        except (KeyError, TypeError, ValueError):
            # График — вторичен: если данных для карточек нет, показываем таблицу.
            st.caption("График для этого результата недоступен — смотрите таблицу.")
        return

    try:
        chart = build_chart(decision, result)
    except (KeyError, TypeError, ValueError):
        chart = None
    if chart is None:
        st.caption("График для этого результата недоступен — смотрите таблицу.")
        return
    st.altair_chart(chart, use_container_width=True)


def render_clarification(decision: AgentDecision) -> None:
    """Показать уточняющий вопрос модели и варианты ответа."""
    clarification: ClarificationPlan | None = decision.clarification
    if clarification is not None:
        question = clarification.question
        suggestions = list(clarification.suggestions)
    else:
        question = str(decision.dsl.get("question", "Уточните вопрос"))
        suggestions = [str(item) for item in decision.dsl.get("suggestions", [])]
    st.info(f"Нужно уточнение: {question}")
    if suggestions:
        st.markdown("**Варианты:**")
        for suggestion in suggestions:
            st.markdown(f"- {suggestion}")


def render_error(decision: AgentDecision) -> None:
    """Дружелюбная ошибка + список доступных интентов (fallback по ТЗ)."""
    st.error(
        "Не удалось разобрать вопрос. "
        "Попробуйте переформулировать или выбрать один из примеров ниже."
    )
    if decision.error:
        st.caption(f"Детали: {decision.error}")
    st.markdown("**Что я умею:**")
    for name in INTENT_NAMES:
        st.markdown(f"- {INTENT_TITLES[name]}: «{INTENT_EXAMPLES[name]}»")
    with st.expander("Технические детали"):
        st.code(decision.raw_response or decision.error or "нет ответа модели")


def render_result(decision: AgentDecision, result: pd.DataFrame) -> None:
    """Показать заголовок, сводку, таблицу, график и технические детали."""
    st.subheader(INTENT_TITLES.get(decision.intent or "", decision.intent or "Результат"))
    st.write(build_summary(decision, result))
    st.dataframe(
        build_result_table(decision, result),
        hide_index=True,
        column_config=build_column_config(decision, result),
    )
    render_chart(decision, result)
    with st.expander("Технические детали"):
        st.caption(
            "JSON-DSL, который вернула LLM, и режим расчёта — для разработчиков "
            "и проверки. На ответ пользователю не влияет."
        )
        st.markdown("**JSON-DSL:**")
        st.json(decision.dsl)
        st.markdown(
            f"**Интент:** `{decision.intent}` · **Режим:** "
            f"{'LLM' if decision.source == 'llm' else 'офлайн-правила'}"
        )
        if decision.raw_response:
            st.markdown("**Сырой ответ модели:**")
            st.code(decision.raw_response[:4000])


def render_history() -> None:
    """Показать историю диалога (компактно, без повторных таблиц)."""
    for turn in st.session_state.get("history", []):
        if turn.get("role") == "user":
            with st.chat_message("user"):
                st.write(turn["content"])
        else:
            with st.chat_message("assistant"):
                st.caption(turn["content"])


def clear_question() -> None:
    """Очистить поле вопроса (callback кнопки «✖️»).

    Callback выполняется до перерисовки страницы, поэтому запись в
    ``st.session_state`` не конфликтует с уже созданным виджетом ``question_input``
    (прямая запись после создания виджета в том же прогоне запрещена Streamlit).
    """
    st.session_state["question_input"] = ""


def render_question_input() -> tuple[str, bool]:
    """Отрисовать поле вопроса с кнопкой быстрой очистки.

    Отказ от ``st.form`` даёт возможность менять состояние поля без отправки:
    кнопка «✖️» очищает ввод через callback, а обычная кнопка «Спросить»
    отправляет вопрос.

    Returns
    -------
    tuple[str, bool]
        Текст вопроса и признак нажатия «Спросить».
    """
    question_col, clear_col = st.columns([5, 1], vertical_alignment="bottom")
    with question_col:
        question = st.text_input(
            "Вопрос к данным",
            key="question_input",
            placeholder="Например: какие 5 товаров продаются хуже остальных за 2024 год?",
            label_visibility="collapsed",
        )
    with clear_col:
        st.button("✖️", help="Очистить поле", on_click=clear_question)

    return question, st.button("Спросить", type="primary")


# --------------------------------------------------------------------------- #
# Выбор планировщика
# --------------------------------------------------------------------------- #
def select_planner(use_llm: bool, settings: LLMSettings) -> LLMPlanner | RuleBasedPlanner:
    """Выбрать планировщик: LLM при явном включении и настроенном провайдере.

    Parameters
    ----------
    use_llm:
        Флаг переключателя в сайдбаре: пользователь хочет использовать LLM.
    settings:
        Настройки LLM из окружения/``.env`` (провайдер, ключ, модель).

    Returns
    -------
    LLMPlanner | RuleBasedPlanner
        :class:`LLMPlanner`, если LLM включена и настроена (есть провайдер и
        ключ), иначе детерминированные офлайн-правила :class:`RuleBasedPlanner`.

    Notes
    -----
    Флаг не может включить LLM, если она не настроена: без ключа приложение
    остаётся в офлайн-режиме и не делает сетевых вызовов.
    """
    if use_llm and settings.is_configured:
        return LLMPlanner(settings)
    return RuleBasedPlanner()


# --------------------------------------------------------------------------- #
# Боковая панель
# --------------------------------------------------------------------------- #
def render_sidebar() -> tuple[str, Any, bool]:
    """Отрисовать боковую панель: источник витрины, режим LLM, сброс истории.

    Returns
    -------
    tuple[str, Any, bool]
        Выбранный источник данных, загруженный файл (для ``SOURCE_UPLOAD``) и
        флаг «использовать LLM». Если LLM не настроена, переключатель
        заблокирован, а флаг принудительно ``False`` — работают офлайн-правила.
    """
    st.sidebar.title("Данные")
    source_kind = st.sidebar.radio(
        "Источник витрины",
        options=[SOURCE_SAMPLE, SOURCE_UPLOAD],
        index=0,
        help=(
            "По умолчанию — синтетические демо-данные (без реальных выгрузок 1С). "
            "Свой CSV из 1С или Excel можно загрузить вторым пунктом."
        ),
    )
    uploaded = None
    if source_kind == SOURCE_UPLOAD:
        uploaded = st.sidebar.file_uploader(
            "CSV из 1С или Excel-выгрузка",
            type=["csv", "xlsx", "xls"],
        )
    st.sidebar.divider()

    try:
        settings = settings_from_env()
    except ValueError as exc:
        st.sidebar.error(str(exc))
        settings = None
    llm_configured = settings is not None and settings.is_configured

    use_llm = st.sidebar.toggle(
        "Использовать LLM (DeepSeek/OpenAI)",
        value=True,
        disabled=not llm_configured,
        help=(
            "Если выключено, вопросы разбираются офлайн-правилами без вызова API."
            if llm_configured
            else "LLM не настроена: задайте LLM_PROVIDER и LLM_API_KEY (см. .env.example)."
        ),
    )
    if not llm_configured:
        # Заблокированный переключатель не должен включать LLM: работают правила.
        use_llm = False
        st.sidebar.info(
            "Офлайн-режим: вопрос разбирается детерминированными правилами (без LLM). "
            "Для LLM задайте LLM_PROVIDER и LLM_API_KEY перед запуском."
        )
    elif use_llm:
        st.sidebar.caption(f"LLM: `{settings.provider}` / `{settings.model}`")
    else:
        st.sidebar.caption("Включены офлайн-правила: API не вызывается.")

    if st.sidebar.button("Очистить историю"):
        st.session_state["history"] = []
    st.sidebar.divider()
    return source_kind, uploaded, use_llm


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> None:
    """Точка входа Streamlit: собрать страницу и обработать вопрос."""
    st.set_page_config(page_title=APP_TITLE, page_icon="📊", layout="wide")
    st.title("📊 " + APP_TITLE)
    st.caption(
        "LLM не считает числа: она переводит вопрос в JSON-DSL, "
        "а расчёты выполняет pandas. Выручка по умолчанию — без НДС."
    )

    try:
        check_schema_sync()
    except RuntimeError as exc:
        st.error(f"Схема интентов рассинхронизирована: {exc}")
        st.stop()

    st.session_state.setdefault("history", [])
    source_kind, uploaded, use_llm = render_sidebar()

    try:
        settings = settings_from_env()
    except ValueError:
        # Ошибка уже показана в сайдбаре; для расчётов остаёмся в офлайне.
        settings = LLMSettings()

    try:
        dataset, source_label = load_dataframe(source_kind, uploaded)
    except (ValueError, FileNotFoundError, KeyError, RuntimeError) as exc:
        st.error(f"Не удалось загрузить данные: {exc}")
        st.stop()

    render_dataset_quality(dataset)
    st.sidebar.caption(
        f"Строк: {len(dataset)} · товаров: {dataset[PRODUCT].nunique()} · "
        f"контрагентов: {dataset[COUNTERPARTY].nunique()}"
    )

    render_history()
    profile = data_profile(dataset, source_label)

    question, submitted = render_question_input()

    if submitted:
        if not question.strip():
            st.warning("Введите вопрос.")
            st.stop()
        planner = select_planner(use_llm, settings)
        with st.spinner("Разбираю вопрос…"):
            decision = planner.plan(question, st.session_state["history"], profile)
        if decision.intent == "clarification":
            render_clarification(decision)
        elif not decision.ok:
            render_error(decision)
        else:
            try:
                result = dispatch_intent(decision.intent or "", dataset, decision.params)
            except (ValueError, TypeError, KeyError) as exc:
                st.error("Не удалось выполнить расчёт по этому запросу.")
                st.caption(f"Детали: {exc}")
                st.markdown("**Доступные вопросы:**")
                for name in INTENT_NAMES:
                    st.markdown(f"- {INTENT_TITLES[name]}: «{INTENT_EXAMPLES[name]}»")
                with st.expander("Технические детали"):
                    st.json(decision.dsl)
            else:
                render_result(decision, result)
                summary = build_summary(decision, result)
                st.session_state["history"].append({"role": "user", "content": question})
                st.session_state["history"].append(
                    {"role": "assistant", "content": f"{INTENT_TITLES.get(decision.intent or '', '')}: {summary}"}
                )

    st.divider()
    st.caption(
        f"Источник: {source_kind} · интентов: {len(INTENT_REGISTRY)} · "
        f"доступные интенты: {', '.join(INTENT_NAMES)}"
    )


if __name__ == "__main__":
    main()
