"""Streamlit-приложение «ИИ-ассистент для анализа данных из 1С».

Поток: вопрос пользователя → LLM (или офлайн-правила) → JSON-DSL →
валидация Pydantic → :func:`analytics.dispatch_intent` → pandas → таблица,
график, текстовая сводка и блок «Технические детали» (JSON-DSL).

Запуск::

    streamlit run app.py

Переменные окружения для LLM (опционально)::

    LLM_PROVIDER=deepseek|openai|ollama|gigachat|none
    LLM_API_KEY=...
    LLM_MODEL=deepseek-chat
    LLM_BASE_URL=https://api.deepseek.com/v1

``LLM_MODEL``/``LLM_BASE_URL`` — сокращения для ``<...>_<LLM_PROVIDER>``: они
принадлежат провайдеру по умолчанию и на другого провайдера не распространяются
(см. ``onec_analyst.llm_agent._pick_setting``).

Без ``LLM_PROVIDER`` приложение работает в офлайн-режиме на детерминированном
разборе типовых вопросов — удобно для демо и приёмки без ключа API.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

# Локальный запуск без ``pip install -e .``: пакет лежит в src/.
_SRC = Path(__file__).resolve().parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import altair as alt
import pandas as pd
import streamlit as st

from onec_analyst.analytics import (
    CHECK,
    COUNTERPARTY,
    DATE,
    DELTA,
    DIRECTION,
    INTENT_REGISTRY,
    PERIOD,
    PRODUCT,
    QUANTITY,
    RANK_P1,
    RANK_P2,
    REVENUE,
    ROWS_COUNT,
    STATUS,
    VALUE,
    get_data_quality,
)
from onec_analyst.clarification import (
    SOURCE_LLM,
    choose_clarified_text,
    force_interpretation,
)
from onec_analyst.data_loader import load_and_clean_1c_report
from onec_analyst.dispatcher import (
    AssistantResult,
    DispatchError,
    process_request,
    select_planner as _select_planner,
)
from onec_analyst.llm_agent import (
    INTENT_EXAMPLES,
    INTENT_NAMES,
    AgentDecision,
    ClarificationPlan,
    GigaChatClient,
    LLMPlanner,
    LLMSettings,
    ProviderOption,
    RuleBasedPlanner,
    check_schema_sync,
    make_client,
    provider_catalog,
    settings_from_env,
)
from onec_analyst.onec_source import load_sample_vitrina, load_vitrina
from onec_analyst.pipeline import execute, extract_entities
from onec_analyst.agent.state import CallBudgetGuard, CompositeGuard
from onec_analyst.rate_limiter import (
    MAX_GLOBAL,
    MAX_PER_SESSION,
    LimitStatus,
    RateLimiter,
    RateLimiterError,
    RateLimiterGuard,
)
from onec_analyst.session import ConversationState
from onec_analyst.summary import (
    QUANTITY_UNIT,
    REVENUE_UNIT,
    _direction_label,
    _money,
    _period_value_label,
    _status_label,
    build_summary,
    format_metric,
    format_percent,
    format_quantity,
    metric_unit,
)

APP_TITLE = "ИИ-ассистент анализа данных 1С"

#: Env-флаг: показать кнопку ручного сброса лимита сессии (по умолчанию выкл.).
_RATE_LIMIT_RESET_ENV = "RATE_LIMIT_ALLOW_RESET"


def _rate_limit_reset_allowed() -> bool:
    """Разрешён ли ручной сброс лимита LLM (только при явном env)."""
    return os.environ.get(_RATE_LIMIT_RESET_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def format_usage_caption(limit_status: LimitStatus) -> str:
    """Текст счётчика LLM в сайдбаре."""
    return (
        f"Использовано вызовов ИИ: "
        f"{limit_status.used_session}/{limit_status.limit_session}"
    )


def fill_usage_caption(slot: Any, limit_status: LimitStatus | None) -> None:
    """Записать или очистить подпись счётчика (``st.empty``-слот)."""
    if limit_status is None:
        slot.empty()
    else:
        slot.caption(format_usage_caption(limit_status))


def fill_session_limit_warning(slot: Any, limit_status: LimitStatus | None) -> None:
    """Показать/убрать плашку исчерпания лимита сессии в сайдбаре."""
    if limit_status is not None and limit_status.used_session >= limit_status.limit_session:
        slot.warning("Лимит ИИ в этой сессии исчерпан — включён офлайн-режим.")
    else:
        slot.empty()


def record_llm_usage(
    limiter: RateLimiter,
    session_id: str,
    usage_slot: Any,
    warn_slot: Any,
) -> LimitStatus | None:
    """Атомарно зарезервировать один успешный вызов LLM и обновить счётчик/плашку.

    Используется для отдельных вызовов вне графа (классификатор уточнений):
    вызов уже завершился успешно, поэтому резерв ``try_consume`` сразу остаётся.

    Returns
    -------
    LimitStatus | None
        Обновлённый статус после резерва, либо ``None`` при ошибке SQLite.
    """
    try:
        _, status = limiter.try_consume(
            session_id,
            max_per_session=MAX_PER_SESSION,
            max_global=MAX_GLOBAL,
        )
    except RateLimiterError as exc:
        st.warning(f"⚠️ Не удалось учесть вызов ИИ: {exc}")
        return None
    fill_usage_caption(usage_slot, status)
    fill_session_limit_warning(warn_slot, status)
    return status


def refresh_usage_caption(
    limiter: RateLimiter,
    session_id: str,
    usage_slot: Any,
    warn_slot: Any,
) -> LimitStatus | None:
    """Перечитать счётчик из базы и обновить подпись/плашку (без инкремента).

    Используется после агентного запроса: guard уже учёл каждый вызов, здесь
    только синхронизируем UI с фактическим счётчиком.
    """
    try:
        refreshed = limiter.check_limit(
            session_id,
            max_per_session=MAX_PER_SESSION,
            max_global=MAX_GLOBAL,
        )
    except RateLimiterError as exc:
        st.warning(f"⚠️ Не удалось прочитать счётчик ИИ: {exc}")
        return None
    fill_usage_caption(usage_slot, refreshed)
    fill_session_limit_warning(warn_slot, refreshed)
    return refreshed


#: Сколько уточнений подряд показываем, прежде чем выполнить ближайшую
#: детерминированную интерпретацию (защита от цикла уточнений).
MAX_CLARIFICATIONS = 2


SOURCE_SAMPLE = "Демо-данные: sample_data.csv"
SOURCE_1C = "1C: 1c_export/sales_1c.csv"
SOURCE_UPLOAD = "Загрузить свой файл"

DATASET_SAMPLE = "sample_data.csv"
DATASET_1C = "1c_export/sales_1c.csv"

INTENT_TITLES: dict[str, str] = {
    "top_bottom_products": "Топ/антитоп товаров",
    "compare_top_products": "Сравнение топов (L4L)",
    "negative_revenue": "Отрицательная выручка / возвраты",
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
def load_dataset_1c(mtime_ns: int | None = None) -> pd.DataFrame:
    """Загрузить локальную выгрузку из 1С (``1c_export/sales_1c.csv``).

    ``mtime_ns`` участвует в ключе кеша: после перезаписи CSV следующим
    запросом приложение перечитает файл, даже если процесс не перезапускали.
    """
    return load_vitrina(DATASET_1C)


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


def load_dataframe(kind: str, uploaded: Any) -> tuple[pd.DataFrame | None, str]:
    """Выбрать и загрузить витрину по выбору в боковой панели.

    Если выбран источник 1С, но локального файла выгрузки нет, показываем
    предупреждение и возвращаем ``None`` — приложение продолжает работать,
    пользователь может переключиться на демо-данные или загрузку файла.
    """
    if kind == SOURCE_SAMPLE:
        return load_dataset_sample(), "демо-данные"
    if kind == SOURCE_1C:
        path = Path(DATASET_1C)
        if not path.is_file():
            st.warning(
                "Файл выгрузки из 1С не найден. Пожалуйста, используйте скрипт "
                "`python -m onec_analyst.onec_source` для выгрузки данных или выберите Демо-данные / "
                "Загрузите свой файл."
            )
            return None, "1C"
        return load_dataset_1c(path.stat().st_mtime_ns), "1C"
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
# Таблица: русские подписи и форматы
# --------------------------------------------------------------------------- #
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
    "rank_p1": "Место в периоде 1",
    "rank_p2": "Место в периоде 2",
    "status": "Статус",
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


def _period_short_label(period: Any) -> str:
    """Короткая подпись периода для заголовков L4L: «2023», «03.2024», «01.2024–06.2024»."""
    try:
        start, end = period
        start_ts = pd.Timestamp(start)
        end_ts = pd.Timestamp(end)
        if (start_ts.month, start_ts.day) == (1, 1) and (end_ts.month, end_ts.day) == (12, 31):
            if start_ts.year == end_ts.year:
                return str(start_ts.year)
        if (start_ts.year, start_ts.month) == (end_ts.year, end_ts.month):
            return start_ts.strftime("%m.%Y")
        return f"{start_ts.strftime('%d.%m.%Y')}–{end_ts.strftime('%d.%m.%Y')}"
    except (TypeError, ValueError):
        return ""


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
        short1 = _period_short_label(params["period1"])
        labels[RANK_P1] = f"Место {short1}".strip()
        labels[f"{metric}_p1"] = f"{labels.get(metric, metric)} ({short1})" if short1 else labels.get(metric, metric)
    if "period2" in params:
        labels["period2_value"] = _period_value_label(
            params["period2"], unit, f"Период 2, {unit}"
        )
        short2 = _period_short_label(params["period2"])
        labels[RANK_P2] = f"Место {short2}".strip()
        labels[f"{metric}_p2"] = f"{labels.get(metric, metric)} ({short2})" if short2 else labels.get(metric, metric)
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
    if STATUS in display.columns:
        # Статусы L4L: цветной маркер + русская подпись; сортировка по тексту.
        display[STATUS] = display[STATUS].map(_status_label)
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
        elif column == REVENUE or column in (
            "period1_value",
            "period2_value",
            DELTA,
            f"{metric}_p1",
            f"{metric}_p2",
        ):
            config[column] = _number_column(label, step=money_step)
        elif column == QUANTITY:
            config[column] = _number_column(label)
        elif column in ("share_pct", "delta_pct"):
            config[column] = _number_column(label, step=0.01)
        elif column in ("rank", RANK_P1, RANK_P2, ROWS_COUNT, "undated_rows"):
            config[column] = _number_column(label, step=1)
        else:
            config[column] = st.column_config.TextColumn(label)
    return config


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
    st.sidebar.caption(
        "ℹ️ Возвраты и сторно уменьшают итоговую выручку. "
        "Строки с нулевой выручкой не влияют на суммы."
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

    if intent == "compare_top_products":
        value_p1 = f"{metric}_p1"
        value_p2 = f"{metric}_p2"
        if (
            PRODUCT not in result.columns
            or value_p1 not in result.columns
            or value_p2 not in result.columns
        ):
            return None
        data = result.copy()
        order = [str(item) for item in data[PRODUCT].tolist()]
        label_p1 = labels.get(value_p1, value_p1)
        label_p2 = labels.get(value_p2, value_p2)
        # Wide -> long: два бара рядом (grouped bar) по каждому товару.
        melted = data.melt(
            id_vars=[PRODUCT],
            value_vars=[value_p1, value_p2],
            var_name="comparison_period",
            value_name=metric,
        )
        melted["comparison_period"] = melted["comparison_period"].map(
            {value_p1: label_p1, value_p2: label_p2}
        )
        return (
            alt.Chart(melted)
            .mark_bar()
            .encode(
                x=alt.X(
                    PRODUCT,
                    type="nominal",
                    title=labels.get(PRODUCT, PRODUCT),
                    sort=order,
                ),
                xOffset=alt.XOffset(
                    "comparison_period",
                    sort=[label_p1, label_p2],
                    title="Период",
                ),
                y=alt.Y(metric, type="quantitative", title=metric_title),
                color=alt.Color("comparison_period", type="nominal", title="Период"),
            )
            .properties(height=_chart_height(len(result)))
        )

    if intent == "negative_revenue":
        dim = str(params.get("group_by", PRODUCT))
        if dim not in result.columns or REVENUE not in result.columns:
            return None
        return (
            alt.Chart(result)
            .mark_bar(color="#d62728")
            .encode(
                x=alt.X(
                    REVENUE,
                    type="quantitative",
                    title=labels.get(REVENUE, f"Выручка без НДС, {REVENUE_UNIT}"),
                ),
                y=alt.Y(dim, type="nominal", title=labels.get(dim, dim), sort="x"),
            )
            .properties(height=_chart_height(len(result)))
        )

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


def apply_suggestion(text: str | None = None) -> None:
    """Callback выбора варианта уточнения: запомнить ответ и запустить перерисовку.

    Текст в поле подставляет :func:`main` до создания виджета: он вызывает
    :func:`clarification.choose_clarified_text` — правила решают, оставить только
    вторую часть (полный вопрос) или склеить её с исходной (фрагмент), а спорный
    случай уходит в LLM-классификатор. Поэтому сетевой вызов не делается внутри
    callback и поле не конфликтует с уже созданным виджетом.
    """
    choice = text if text is not None else st.session_state.get("clarification_choice")
    if not choice:
        return
    st.session_state["clarification_answer"] = str(choice)


def render_suggestion_pills(suggestions: list[str]) -> None:
    """Показать варианты ответа кликабельными «таблетками» (pills).

    ``st.pills`` доступен с Streamlit 1.36; если компонента нет (старая версия
    или урезанная сборка), используется запасной вариант — компактные кнопки
    ``type="tertiary"``, визуально похожие на гиперссылки.
    """
    if hasattr(st, "pills"):
        st.pills(
            "Варианты ответа",
            options=suggestions,
            selection_mode="single",
            key="clarification_choice",
            on_change=apply_suggestion,
            label_visibility="collapsed",
        )
        return
    for index, suggestion in enumerate(suggestions):
        st.button(
            suggestion,
            key=f"clarification_suggestion_{index}",
            type="tertiary",
            help="Подставить этот вариант в поле вопроса",
            on_click=apply_suggestion,
            args=(suggestion,),
        )


def render_clarification(decision: AgentDecision) -> None:
    """Показать уточняющий вопрос модели и кликабельные варианты ответа.

    Выбор варианта подставляет в поле умный текст: только вторую часть, если она
    сама по себе полный вопрос, или склейку с исходным вопросом, если это фрагмент
    (решение — :func:`clarification.choose_clarified_text`).
    """
    clarification: ClarificationPlan | None = decision.clarification
    if clarification is not None:
        question = clarification.question
        suggestions = list(clarification.suggestions)
    else:
        question = str(decision.dsl.get("question", "Уточните вопрос"))
        suggestions = [str(item) for item in decision.dsl.get("suggestions", [])]
    st.info(f"Нужно уточнение: {question}")
    if suggestions:
        st.caption(
            "Выберите вариант ответа — он подставится в поле вопроса и отправится автоматически."
        )
        render_suggestion_pills(suggestions)


def render_refusal(reason: str | None) -> None:
    """Плашка отказа агента: причина обязательна, расчёт не выполняется.

    Отказ — новое состояние этапа 2 (``status="refuse"``): честное объяснение
    вместо «похожего» ответа. Плашка отличается от уточнения (``st.info``) и
    ошибки (``st.error``): это не сбой, а осознанное ограничение.
    """
    text = (reason or "").strip() or (
        "Запрос не относится к поддерживаемой аналитике продаж."
    )
    st.warning(f"Не могу выполнить этот запрос: {text}")
    st.caption("Числа и таблица не строятся: честный отказ лучше «похожего» ответа.")


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


def render_result(
    decision: AgentDecision,
    result: pd.DataFrame,
    data_profile: dict[str, Any] | None = None,
    *,
    summary: str | None = None,
) -> None:
    """Показать заголовок, сводку, таблицу, график и технические детали."""
    st.subheader(INTENT_TITLES.get(decision.intent or "", decision.intent or "Результат"))
    st.write(summary if summary is not None else build_summary(decision, result, data_profile))
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
        # st.code даёт встроенную кнопку «копировать» в правом верхнем углу блока.
        st.code(
            json.dumps(decision.dsl, ensure_ascii=False, indent=2),
            language="json",
        )
        st.caption("Кнопка копирования JSON — в правом верхнем углу блока.")
        st.markdown(
            f"**Интент:** `{decision.intent}` · **Режим:** "
            f"{'LLM' if decision.source == 'llm' else 'офлайн-правила'} · "
            f"**Статус:** `{decision.status}`"
        )
        if decision.raw_response:
            st.markdown("**Сырой ответ модели:**")
            st.code(decision.raw_response[:4000])


def render_dispatch_error(decision: AgentDecision, exc: BaseException) -> None:
    """Показать ошибку расчёта и доступные вопросы (как раньше в ``main``)."""
    st.error("Не удалось выполнить расчёт по этому запросу.")
    st.caption(f"Детали: {exc}")
    st.markdown("**Доступные вопросы:**")
    for name in INTENT_NAMES:
        st.markdown(f"- {INTENT_TITLES[name]}: «{INTENT_EXAMPLES[name]}»")
    with st.expander("Технические детали"):
        st.code(
            json.dumps(decision.dsl, ensure_ascii=False, indent=2),
            language="json",
        )


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
    Заодно сбрасывается база для склейки уточнения: очистка поля означает новый
    вопрос, а не ответ на предыдущее уточнение.
    """
    st.session_state["question_input"] = ""
    st.session_state.pop("clarification_base", None)
    st.session_state.pop("clarification_answer", None)
    st.session_state["clarification_rounds"] = 0


def render_question_input(*, disabled: bool = False, reason: str = "") -> tuple[str, bool]:
    """Отрисовать поле вопроса: очистка «✖️» и отправка кнопкой или Enter.

    Поле и кнопка «Спросить» находятся внутри ``st.form``: **Enter в поле
    отправляет форму**, то есть работает как обычная отправка вопроса. Кнопка
    «✖️» вынесена за пределы формы, поэтому очистка поля не отправляет запрос
    (это было причиной отказа от формы на шаге 3 — теперь ограничение снято).

    Parameters
    ----------
    disabled:
        Заблокировать «Спросить» (LLM включена, но провайдеров нет).
    reason:
        Почему кнопка заблокирована — подсказка под кнопкой.

    Returns
    -------
    tuple[str, bool]
        Текст вопроса и признак отправки (кнопка «Спросить» или Enter).
    """
    question_col, clear_col = st.columns([5, 1], vertical_alignment="bottom")
    with question_col:
        with st.form("question_form", clear_on_submit=False, border=False):
            question = st.text_input(
                "Вопрос к данным",
                key="question_input",
                placeholder="Например: какие 5 товаров продаются хуже остальных за 2024 год?",
                label_visibility="collapsed",
            )
            submitted = st.form_submit_button(
                "Спросить",
                type="primary",
                disabled=disabled,
                help=reason or None,
            )
        if disabled and reason:
            st.caption(reason)
    with clear_col:
        st.button("✖️", help="Очистить поле", on_click=clear_question)

    return question, submitted


# --------------------------------------------------------------------------- #
# Выбор планировщика
# --------------------------------------------------------------------------- #
def select_planner(use_llm: bool, settings: LLMSettings) -> LLMPlanner | RuleBasedPlanner:
    """Выбрать планировщик (публичная обёртка :mod:`onec_analyst.dispatcher`).

    Оставлена в ``app`` для совместимости: ``tests/test_app_planner.py``
    импортирует её отсюда. Само правило выбора живёт в диспетчере.
    """
    return _select_planner(use_llm, settings)


def _provider_setup_hint(provider: str) -> str:
    """Подсказка, какие переменные ``.env`` нужны конкретному провайдеру."""
    if provider == "gigachat":
        return (
            "LLM_API_KEY_GIGACHAT, GIGACHAT_SCOPE и LLM_TLS_CA_BUNDLE "
            "(сертификат НУЦ Минцифры)"
        )
    if provider == "ollama":
        return (
            "OLLAMA_ENABLED=true (ключ локальной модели не нужен; "
            "модель — OLLAMA_MODEL, адрес — OLLAMA_BASE_URL)"
        )
    return f"LLM_API_KEY_{provider.upper()}"


def _default_provider_index(
    options: list[ProviderOption], default: LLMSettings | None
) -> int:
    """Индекс провайдера по умолчанию; если он недоступен — первый доступный."""

    def index_of(provider: str | None) -> int | None:
        if not provider:
            return None
        for index, option in enumerate(options):
            if option.settings.provider == provider:
                return index
        return None

    if default is not None:
        found = index_of(default.provider)
        if found is not None and options[found].configured:
            return found
    for index, option in enumerate(options):
        if option.configured:
            return index
    return 0


#: Человекочитаемые имена провайдеров для подписи в сайдбаре.
_PROVIDER_TITLES = {
    "openai": "OpenAI",
    "deepseek": "DeepSeek",
    "ollama": "Ollama",
    "gigachat": "GigaChat",
}


def _provider_title(provider: str) -> str:
    """Имя провайдера для UI: ``gigachat`` → ``GigaChat`` (неизвестный — как есть)."""
    return _PROVIDER_TITLES.get(provider, provider)


def _provider_label(option: ProviderOption) -> str:
    """Подпись провайдера в списке: доступный — именем, недоступный — с причиной.

    Причина у всех своя: облачным не хватает ключа/сертификата, а Ollama
    выключается флагом, а не ключом, — «не настроен» про неё вводило бы в
    заблуждение.
    """
    title = _provider_title(option.settings.provider)
    if option.configured:
        return title
    if option.settings.provider == "ollama":
        return f"{title} — выключена в .env"
    return f"{title} — не настроен"


def choose_llm_provider(
    options: list[ProviderOption],
    default: LLMSettings | None,
    *,
    disabled: bool = False,
) -> ProviderOption:
    """Виджет выбора провайдера LLM; вернуть выбранный вариант.

    Показывает всех провайдеров: доступные — обычным именем, недоступные — с
    причиной («— не настроен» / «— выключена в .env»). По умолчанию выбирается
    доступный провайдер из ``LLM_PROVIDER`` (или первый доступный). Позволяет
    держать, например, DeepSeek и GigaChat одновременно.

    ``disabled=True`` — «LLM выключена галочкой»: список виден, но выбрать
    провайдера нельзя.

    В подписи — **только провайдер**, без модели: модель показывается отдельно
    (для GigaChat — селектором «Модель GigaChat», для остальных — подписью
    ``LLM: …``). Иначе в переключателе застывала бы модель из ``.env`` и
    противоречила бы фактически выбранной.
    """
    if not options:
        return ProviderOption(settings=LLMSettings(), configured=False)
    labels = [_provider_label(option) for option in options]
    default_index = _default_provider_index(options, default)
    selected = st.sidebar.selectbox(
        "Провайдер LLM",
        options=labels,
        index=default_index,
        disabled=disabled,
        help=(
            "Какой провайдер использовать для разбора вопроса. Недоступные "
            "показаны с причиной: облачным нужен ключ/сертификат в .env, "
            "Ollama включается флагом OLLAMA_ENABLED."
        ),
    )
    return options[labels.index(selected)]


def _gigachat_chat_models(settings: LLMSettings) -> tuple[str, ...]:
    """Чат-модели GigaChat, реально доступные ключу (``GET /v1/models``).

    Берутся только модели генерации (``type == "chat"``): GigaChat отдаёт в том
    же списке эмбеддинги, но выбрать эмбеддинг моделью чата нельзя.

    Кешируется в ``st.session_state`` на сессию: запрос не повторяется на каждом
    rerun, но и не протекает между сессиями (и между тестами ``AppTest``).
    Ошибку не маскируем исключением на весь UI: возвращаем пустой кортеж, и
    селектор моделей просто не рисуется.
    """
    cache_key = f"_gigachat_models:{settings.base_url}"
    if cache_key in st.session_state:
        return tuple(st.session_state[cache_key])
    try:
        client = make_client(settings)
        if not isinstance(client, GigaChatClient):
            return ()
        models = tuple(client.chat_models())
    except Exception:  # сеть, TLS, HTTP, формат ответа — список необязателен
        models = ()
    st.session_state[cache_key] = models
    return models


def choose_gigachat_model(settings: LLMSettings) -> LLMSettings:
    """Выбрать модель GigaChat в сайдбаре; вернуть настройки с выбором на сессию.

    Список — факт из API, а не из документации: разным ключам доступны разные
    модели. Выбор живёт в ``st.session_state`` и **не** пишется в ``.env``:
    приложение не редактирует файл с секретами.

    Если список получить не удалось (нет сети/ключа/сертификата), функция
    возвращает настройки как есть — тогда действует ``LLM_MODEL_GIGACHAT`` из
    ``.env``, а селектор не рисуется.

    Parameters
    ----------
    settings:
        Настройки выбранного провайдера.

    Returns
    -------
    LLMSettings
        Те же настройки, но с моделью, выбранной в списке (для GigaChat).
    """
    if settings.provider != "gigachat":
        return settings
    models = list(_gigachat_chat_models(settings))
    if not models:
        return settings
    # Модель из ``.env`` может отсутствовать в списке API (уровень не тот, тариф
    # другой). Тогда показываем её как выбранную: молча подменить модель на
    # первую из списка — значит отправить запрос не тем именем, чем просили.
    if settings.model not in models:
        models.insert(0, settings.model)
    selected = st.sidebar.selectbox(
        "Модель GigaChat", options=models, index=models.index(settings.model)
    )
    if selected == settings.model:
        return settings
    return dataclasses.replace(settings, model=selected)


# --------------------------------------------------------------------------- #
# Боковая панель
# --------------------------------------------------------------------------- #
#: Подсказка, когда LLM включена галочкой, но ни один провайдер не доступен.
NO_PROVIDERS_MESSAGE = (
    "Нет доступных провайдеров LLM: задайте ключ в `.env` или `OLLAMA_ENABLED=true`."
)

#: Напоминание на время вызова LLM: обновление вкладки создаёт новую сессию.
#: Локальная модель на CPU отвечает минутами, и именно там потеря ответа обиднее
#: всего; для облака плашка мелькает незаметно и после ответа не остаётся.
LONG_RUN_NOTICE = (
    "Идёт расчёт. Не обновляйте страницу и не закрывайте вкладку: "
    "текущий ответ не сохранится, а вызов будет засчитан."
)


def render_sidebar(
    limiter: RateLimiter,
    session_id: str,
    limit_status: LimitStatus | None,
) -> tuple[str, Any, bool, Any, Any, LLMSettings, bool, str]:
    """Отрисовать боковую панель: источник витрины, режим LLM, сброс истории.

    Parameters
    ----------
    limiter:
        Учёт вызовов LLM (SQLite); нужен для опционального сброса лимита.
    session_id:
        Идентификатор текущей сессии (``uuid4``), ключ учёта лимитов.
    limit_status:
        Текущий статус лимитов; ``None``, если учёт недоступен.

    Returns
    -------
    tuple[str, Any, bool, Any, Any, LLMSettings, bool, str]
        Источник, файл, флаг LLM, слот счётчика и слот плашки лимита
        (``st.empty``), чтобы обновить UI после ``increment`` в том же прогоне,
        настройки выбранного провайдера, признак «спросить нельзя» (галочка
        включена, а провайдер недоступен/их нет) и текст причины.
    """
    st.sidebar.title("Данные")
    source_kind = st.sidebar.radio(
        "Источник витрины",
        options=[SOURCE_SAMPLE, SOURCE_1C, SOURCE_UPLOAD],
        index=0,
        help=(
            "По умолчанию — синтетические демо-данные. Источник 1С доступен, если "
            "локально есть 1c_export/sales_1c.csv (создаётся onec_analyst.onec_source). "
            "Свой CSV из 1С или Excel можно загрузить третьим пунктом."
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
        default_settings = settings_from_env()
    except ValueError as exc:
        st.sidebar.error(str(exc))
        default_settings = None

    try:
        provider_options = provider_catalog()
    except ValueError:
        provider_options = []
    configured_options = [option for option in provider_options if option.configured]
    any_available = bool(configured_options)

    # Галочка — выбор пользователя, а не индикатор наличия ключей: она не
    # блокируется. ON без единого провайдера возможен и обрабатывается ниже
    # (кнопка «Спросить» блокируется с явным сообщением).
    use_llm = st.sidebar.toggle(
        "Использовать LLM",
        value=any_available,
        help=(
            "Включено — вопрос разбирает выбранный провайдер; выключено — "
            "офлайн-правила без вызова API."
        ),
    )
    # Список показываем всегда: недоступные — с причиной; при выключенной
    # галочке выбор заблокирован («никакую LLM не выбрать»).
    selected_option = choose_llm_provider(
        provider_options, default_settings, disabled=not use_llm
    )
    ask_blocked = use_llm and not any_available
    ask_blocked_reason = NO_PROVIDERS_MESSAGE if ask_blocked else ""
    if not use_llm:
        chosen_settings = LLMSettings()
        st.sidebar.caption("Включены офлайн-правила: API не вызывается.")
    elif ask_blocked:
        chosen_settings = LLMSettings()
        st.sidebar.warning(NO_PROVIDERS_MESSAGE)
    elif selected_option.configured:
        chosen_settings = choose_gigachat_model(selected_option.settings)
        st.sidebar.caption(f"LLM: `{chosen_settings.provider}` / `{chosen_settings.model}`")
    else:
        # Выбран недоступный провайдер: при включённой галочке офлайн-правила
        # запрещены. Не подменяем молча — просим выбрать доступного.
        chosen_settings = LLMSettings()
        provider_name = selected_option.settings.provider
        reason = "выключен в .env" if provider_name == "ollama" else "не настроен"
        ask_blocked = True
        ask_blocked_reason = (
            f"Провайдер «{provider_name}» {reason}: выберите доступного провайдера "
            f"в списке или задайте {_provider_setup_hint(provider_name)} в .env."
        )
        st.sidebar.warning(ask_blocked_reason)

    usage_slot = st.sidebar.empty()
    warn_slot = st.sidebar.empty()
    fill_usage_caption(usage_slot, limit_status)
    fill_session_limit_warning(warn_slot, limit_status)
    if (
        limit_status is not None
        and limit_status.used_session >= limit_status.limit_session
        and _rate_limit_reset_allowed()
        and st.sidebar.button("Сбросить лимит сессии")
    ):
        try:
            limiter.reset_session(session_id)
        except RateLimiterError as exc:
            st.sidebar.error(f"Не удалось сбросить лимит: {exc}")
        else:
            st.rerun()

    if st.sidebar.button("Очистить историю"):
        st.session_state["history"] = []
        # Новый диалог: история, поле вопроса и контекст уточнения.
        st.session_state["question_input"] = ""
        st.session_state["clarification_rounds"] = 0
        st.session_state.pop("clarification_base", None)
        st.session_state.pop("clarification_choice", None)
        st.session_state.pop("clarification_answer", None)
        st.session_state.pop("auto_submit", None)
    st.sidebar.divider()
    return (
        source_kind,
        uploaded,
        use_llm,
        usage_slot,
        warn_slot,
        chosen_settings,
        ask_blocked,
        ask_blocked_reason,
    )


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
    st.session_state.setdefault("llm_session_id", str(uuid.uuid4()))
    llm_session_id = str(st.session_state["llm_session_id"])

    limiter = RateLimiter()
    limit_status: LimitStatus | None = None
    limiter_error: str | None = None
    try:
        limit_status = limiter.check_limit(
            llm_session_id, max_per_session=MAX_PER_SESSION, max_global=MAX_GLOBAL
        )
    except RateLimiterError as exc:
        limiter_error = str(exc)

    if limiter_error is not None:
        st.warning(
            "⚠️ Учёт лимитов ИИ недоступен (база занята) — работаю в офлайн-режиме правил."
        )
    elif limit_status is not None and limit_status.reason == "global":
        st.error(
            "🔴 Дневной лимит ИИ-запросов исчерпан. "
            "Система работает в офлайн-режиме."
        )

    (
        source_kind,
        uploaded,
        use_llm,
        usage_slot,
        warn_slot,
        settings,
        ask_blocked,
        ask_blocked_reason,
    ) = render_sidebar(limiter, llm_session_id, limit_status)

    limit_blocked = limiter_error is not None or (
        limit_status is not None and not limit_status.allowed
    )
    if (
        limit_status is not None
        and limit_status.reason == "session"
        and use_llm
    ):
        st.warning(
            "⚠️ Вы достигли лимита вызовов ИИ в этой сессии. "
            "Переключаюсь на офлайн-режим."
        )

    try:
        dataset, source_label = load_dataframe(source_kind, uploaded)
    except (ValueError, FileNotFoundError, KeyError, RuntimeError) as exc:
        st.error(f"Не удалось загрузить данные: {exc}")
        st.stop()

    if dataset is None:
        # Предупреждение уже показано в load_dataframe; не останавливаем приложение,
        # чтобы можно было переключиться на демо-данные или загрузку файла.
        st.caption(
            "Переключитесь на «Демо-данные: sample_data.csv» или «Загрузить свой файл» "
            "в сайдбаре."
        )
        return

    render_dataset_quality(dataset)
    st.sidebar.caption(
        f"Строк: {len(dataset)} · товаров: {dataset[PRODUCT].nunique()} · "
        f"контрагентов: {dataset[COUNTERPARTY].nunique()}"
    )

    render_history()
    profile = data_profile(dataset, source_label)

    # Выбранный вариант уточнения: решаем, показать полный склеенный запрос или
    # только вторую часть (см. clarification.choose_clarified_text). Текст
    # записывается в поле ДО создания виджета и одинаково уходит в расчёт и историю.
    # LLM для планировщика и классификатора уточнений — только если не упёрлись в лимит.
    llm_allowed = use_llm and not limit_blocked

    pending_answer = st.session_state.pop("clarification_answer", None)
    if pending_answer:
        base = str(st.session_state.get("clarification_base") or "")
        clarification_notice = st.empty()
        try:
            with st.spinner("Уточняю запрос…"):
                clarification_notice.caption(LONG_RUN_NOTICE)
                resolved = choose_clarified_text(
                    base,
                    str(pending_answer),
                    use_llm=llm_allowed,
                    settings=settings,
                    data_profile=profile,
                )
        finally:
            clarification_notice.empty()
        st.session_state["question_input"] = resolved.text
        st.session_state["clarification_source"] = resolved.source
        st.session_state["auto_submit"] = True
        # Классификатор — отдельный вызов API; учитываем в лимите, как и plan().
        if resolved.source == SOURCE_LLM:
            refreshed = record_llm_usage(
                limiter, llm_session_id, usage_slot, warn_slot
            )
            if refreshed is not None and not refreshed.allowed:
                limit_blocked = True
                llm_allowed = False

    question, submitted = render_question_input(
        disabled=ask_blocked,
        reason=ask_blocked_reason,
    )

    if st.session_state.get("auto_submit"):
        # Флаг выставлен выбором варианта уточнения (pills/кнопка-ссылка).
        # Сбрасываем его до расчёта, чтобы не было повторной отправки на
        # следующем прогоне и бесконечного цикла; выбранный вариант больше не нужен.
        st.session_state["auto_submit"] = False
        st.session_state.pop("clarification_choice", None)
        st.session_state.pop("clarification_base", None)
        submitted = True

    if submitted:
        if ask_blocked:
            # Enter в поле отправляет форму даже при заблокированной кнопке —
            # не даём уйти в расчёт без провайдера.
            st.warning(ask_blocked_reason)
            st.stop()
        if not question.strip():
            st.warning("Введите вопрос.")
            st.stop()

        # Память диалога — отдельно от истории сообщений. Счётчик уточнений
        # остаётся один: живёт в st.session_state и синхронизируется с ядром.
        conversation = st.session_state.setdefault(
            "conversation_state", ConversationState()
        )
        conversation.clarification_rounds = int(
            st.session_state.get("clarification_rounds", 0)
        )

        outcome: AssistantResult | None = None
        # Композитный guard: потолок 3 вызова на запрос + лимит сессии/дня (15/100).
        # Каждый вызов графа (resolve_context/agent_decision/fix_dsl) резервирует
        # слот атомарно; при сетевом сбое резерв возвращается.
        llm_guard = CompositeGuard(
            CallBudgetGuard(3),
            RateLimiterGuard(limiter, llm_session_id),
        )
        notice_slot = st.empty()
        try:
            with st.spinner("Разбираю вопрос…"):
                notice_slot.caption(LONG_RUN_NOTICE)
                outcome = process_request(
                    question,
                    llm_allowed,
                    conversation,
                    dataset,
                    profile,
                    settings=settings,
                    history=st.session_state["history"],
                    llm_guard=llm_guard,
                )
        except DispatchError as exc:
            render_dispatch_error(exc.decision, exc.original)
            outcome = None
        finally:
            # Плашка живёт только на время вызова: после ответа она не должна
            # оставаться на экране.
            notice_slot.empty()

        if outcome is not None:
            decision: AgentDecision | None = outcome.as_decision()
            # Лимит уже учтён guard'ом на каждый вызов; синхронизируем счётчик UI
            # с фактическим значением в базе (без повторного инкремента).
            refreshed = refresh_usage_caption(limiter, llm_session_id, usage_slot, warn_slot)
            if refreshed is not None and not refreshed.allowed:
                limit_blocked = True
                llm_allowed = False

            if decision.intent == "clarification":
                rounds = int(st.session_state.get("clarification_rounds", 0))
                if rounds >= MAX_CLARIFICATIONS:
                    # Лимит уточнений исчерпан: вместо нового круга — детерминированная
                    # ближайшая интерпретация, чтобы пользователь гарантированно получил
                    # ответ (составной запрос сводится к одному показателю/периоду).
                    forced = force_interpretation(question, data_profile=profile)
                    st.session_state["clarification_rounds"] = 0
                    conversation.clarification_rounds = 0
                    st.session_state.pop("clarification_base", None)
                    if forced is not None:
                        if forced.intent == "compare_top_products":
                            st.info(
                                "Сравниваю два периода: показываю, кто вошёл в топ, "
                                "кто выпал и как изменились места."
                            )
                        else:
                            st.warning(
                                "Два отдельных рейтинга в одном ответе я пока не строю. "
                                "Показываю один рейтинг за запрошенный период. "
                                "Чтобы сравнить годы, задайте вопрос отдельно."
                            )
                        decision = forced
                        outcome = None
                    else:
                        st.warning(
                            "Не удалось однозначно разобрать вопрос даже после уточнений. "
                            "Задайте, пожалуйста, один показатель и один период."
                        )
                        render_error(decision)
                        decision = None
                else:
                    st.session_state["clarification_rounds"] = rounds + 1
                    conversation.clarification_rounds = rounds + 1
                    # Запоминаем исходный вопрос: выбранный вариант уточнения будет
                    # склеен с ним, чтобы в поле и в запросе не терялась первая часть.
                    st.session_state["clarification_base"] = question
                    render_clarification(decision)
                    decision = None

            if (
                decision is not None
                and outcome is not None
                and outcome.status == "refuse"
            ):
                # Отказ агента: причина есть, расчета нет, счётчик уточнений
                # сбрасывается — это не незавершённый диалог.
                render_refusal(outcome.refusal_reason)
                st.session_state["clarification_rounds"] = 0
                conversation.clarification_rounds = 0
                st.session_state.pop("clarification_base", None)
                decision = None

            if decision is not None and not decision.ok:
                render_error(decision)
                st.session_state["clarification_rounds"] = 0
                conversation.clarification_rounds = 0
            elif decision is not None:
                result: pd.DataFrame | None
                summary = ""
                if outcome is not None:
                    result = outcome.result
                    summary = outcome.summary
                else:
                    # Принудительная интерпретация после лимита уточнений:
                    # считаем её тем же ядром, что и обычный запрос.
                    try:
                        execution = execute(decision, dataset, profile)
                    except (ValueError, TypeError, KeyError) as exc:
                        render_dispatch_error(decision, exc)
                        result = None
                    else:
                        result = execution.result
                        summary = execution.summary
                        conversation.record_result(
                            decision, extract_entities(decision, result)
                        )

                if result is not None:
                    render_result(decision, result, profile, summary=summary)
                    st.session_state["history"].append(
                        {"role": "user", "content": question}
                    )
                    st.session_state["history"].append(
                        {
                            "role": "assistant",
                            "content": f"{INTENT_TITLES.get(decision.intent or '', '')}: {summary}",
                        }
                    )
                    st.session_state["clarification_rounds"] = 0
                    conversation.clarification_rounds = 0
                    st.session_state.pop("clarification_base", None)

    st.divider()
    st.caption(
        f"Источник: {source_kind} · интентов: {len(INTENT_REGISTRY)} · "
        f"доступные интенты: {', '.join(INTENT_NAMES)}"
    )


if __name__ == "__main__":
    main()
