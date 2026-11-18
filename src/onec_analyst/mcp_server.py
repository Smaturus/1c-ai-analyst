"""MCP-сервер аналитики 1С: типизированные срезы поверх JSON-DSL.

Сервер — тонкий фасад над :func:`onec_analyst.analytics.dispatch_intent`:
числа считает только движок, сервер собирает ответ по контракту
(:mod:`onec_analyst.answer_contract`) и не даёт агенту вычислять проценты,
подставлять другой срез или читать файлы вне репозитория.

Запуск (stdio)::

    python -m onec_analyst.mcp_server

Инструменты делятся на три группы:

* данные: ``load_dataset``, ``describe_dataset``, ``describe_capabilities``;
* срезы: ``total``, ``top_bottom_products``, ``compare_top_products``,
  ``negative_revenue``, ``timeseries``, ``counterparty_dynamics``,
  ``group_dynamics``;
* произвольного исполнения DSL здесь нет: только перечисленные срезы.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

import pandas as pd
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import ValidationError

from onec_analyst.alerts import (
    ALERTS_ENABLED_ENV,
    AlertPolicy,
    alert_flag_path,
    alert_reason,
    alert_window,
    alerts_enabled,
    collect_returns,
)
from onec_analyst.analytics import dispatch_intent
from onec_analyst.answer_contract import (
    AnswerMeta,
    PeriodResolution,
    format_answer,
    format_period,
    format_refusal,
    resolve_period,
)
from onec_analyst.audit import AuditLog
from onec_analyst.llm_agent import (
    CompareTopsParams,
    CounterpartyDynamicsParams,
    GroupDynamicsParams,
    NegativeRevenueParams,
    TimeseriesParams,
    TopBottomParams,
    TotalParams,
)
from onec_analyst.onec_source import (
    load_sample_vitrina,
    load_vitrina,
    parse_exported_at,
)
from onec_analyst.paths import find_repo_root

__all__ = ["main", "mcp"]

logger = logging.getLogger("onec_analyst.mcp")

MAX_TABLE_ROWS: int = 50
DEFAULT_DATASET: str = "sample_data.csv"
DEFAULT_EXPORT: Path = Path("1c_export") / "sales_1c.csv"
OUTSIDE_REPO_MESSAGE: str = (
    "файл вне каталога проекта — разрешены только витрины внутри репозитория"
)
# Абсолютные пути чужой ОС (windows-диск, UNC): на POSIX `Path` считает их
# относительными и склеил бы с корнем репозитория. Распознаём до склейки.
_FOREIGN_ABSOLUTE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
STATEFUL = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

SUPPORTED: tuple[tuple[str, str], ...] = (
    ("total", "итог за период"),
    ("top_bottom_products", "топ/антитоп товаров за период"),
    ("compare_top_products", "сравнение топов двух периодов"),
    ("negative_revenue", "возвраты/сторно по товарам или контрагентам"),
    ("timeseries", "динамика по дням/месяцам/кварталам/годам"),
    ("counterparty_dynamics", "рост/падение по контрагентам"),
    ("group_dynamics", "рост/падение по товарным группам"),
)
AVAILABLE: tuple[str, ...] = tuple(name for name, _ in SUPPORTED)
UNSUPPORTED: str = (
    "маржа и себестоимость, средний чек, дебиторка, план/факт, остатки, "
    "топ товаров по конкретному контрагенту, разрез группы по месяцам, "
    "запись в 1С, произвольные фильтры"
)

_COLUMN_LABELS: dict[str, str] = {
    "rank": "Место",
    "rank_p1": "Место п1",
    "rank_p2": "Место п2",
    "product": "Товар",
    "product_group": "Товарная группа",
    "counterparty": "Контрагент",
    "revenue": "Выручка",
    "quantity": "Количество",
    "share_pct": "Доля, %",
    "rows_count": "Строк",
    "period": "Период",
    "period_start": "Начало",
    "period_end": "Конец",
    "period1_value": "Период 1",
    "period2_value": "Период 2",
    "revenue_p1": "Выручка п1",
    "revenue_p2": "Выручка п2",
    "delta": "Δ",
    "delta_pct": "Δ, %",
    "direction": "Направление",
    "status": "Статус",
    "undated_rows": "Без даты",
}

_DIRECTION_LABELS: dict[str, str] = {
    "up": "рост",
    "down": "падение",
    "same": "без изменений",
    "new": "новый",
    "out": "выпал",
    "lost": "потеряно",
}

VERBOSE_ENV: str = "ONEC_MCP_VERBOSE"
"""Если переменная окружения истинна, в ответ добавляется строка параметров.

По умолчанию параметры среза не показываются: в чате это шум, они остаются в
аудите (``logger.info``) и доступны для отладки.
"""

_MODELS: dict[str, type[Any]] = {
    "total": TotalParams,
    "top_bottom_products": TopBottomParams,
    "compare_top_products": CompareTopsParams,
    "negative_revenue": NegativeRevenueParams,
    "timeseries": TimeseriesParams,
    "counterparty_dynamics": CounterpartyDynamicsParams,
    "group_dynamics": GroupDynamicsParams,
}


class DatasetError(RuntimeError):
    """Путь к витрине не разрешён или файл недоступен."""


@dataclass(frozen=True)
class Dataset:
    """Загруженная витрина и её происхождение."""

    path: Path
    frame: pd.DataFrame
    loaded_at: datetime


_lock = threading.Lock()
_dataset: Dataset | None = None
_audit = AuditLog()


def _get_dataset() -> Dataset | None:
    with _lock:
        return _dataset


def _set_dataset(path: Path, frame: pd.DataFrame) -> Dataset:
    global _dataset
    dataset = Dataset(path=path, frame=frame, loaded_at=datetime.now().astimezone())
    with _lock:
        _dataset = dataset
    return dataset


def _display_path(path: Path) -> str:
    """Путь относительно корня репозитория — без абсолютных путей в ответах."""
    try:
        return str(path.resolve().relative_to(find_repo_root().resolve()))
    except ValueError:
        return path.name


def _data_bounds(frame: pd.DataFrame) -> tuple[date | None, date | None]:
    if "date" not in frame.columns:
        return None, None
    dates = frame["date"].dropna()
    if dates.empty:
        return None, None
    return dates.min().date(), dates.max().date()


def _as_of_kwargs(frame: pd.DataFrame) -> dict[str, Any]:
    attrs = frame.attrs
    moment = parse_exported_at(attrs.get("exported_at"))
    if moment is None:
        return {}
    if attrs.get("exported_at_source") == "meta":
        return {"as_of": moment}
    return {"file_mtime": moment}


def _default_dataset_path(kind: Literal["auto", "sample", "export"]) -> Path:
    root = find_repo_root()
    if kind == "export":
        return root / DEFAULT_EXPORT
    if kind == "sample":
        return root / DEFAULT_DATASET
    for candidate in (root / DEFAULT_DATASET, root / DEFAULT_EXPORT):
        if candidate.is_file():
            return candidate
    return root / DEFAULT_DATASET


def _resolve_dataset_path(raw: str | None, kind: Literal["auto", "sample", "export"]) -> Path:
    root = find_repo_root().resolve()
    candidate = Path(raw) if raw else _default_dataset_path(kind)
    if not candidate.is_absolute():
        if _FOREIGN_ABSOLUTE.match(str(candidate)):
            raise DatasetError(OUTSIDE_REPO_MESSAGE)
        candidate = root / candidate
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise DatasetError(OUTSIDE_REPO_MESSAGE)
    if resolved.suffix.lower() != ".csv":
        raise DatasetError("поддерживаются только CSV-витрины")
    if not resolved.is_file():
        raise DatasetError(f"файл не найден: {_display_path(resolved)}")
    return resolved


def _detect_kind(path: Path) -> Literal["sample", "export"]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        header = handle.readline()
    return "export" if "revenue_net" in header else "sample"


def _load_frame(path: Path, kind: Literal["auto", "sample", "export"]) -> pd.DataFrame:
    resolved_kind = _detect_kind(path) if kind == "auto" else kind
    if resolved_kind == "export":
        return load_vitrina(str(path))
    return load_sample_vitrina(str(path))


def _ensure_default_dataset() -> None:
    if _get_dataset() is not None:
        return
    for path, kind in (
        (find_repo_root() / DEFAULT_DATASET, "sample"),
        (find_repo_root() / DEFAULT_EXPORT, "export"),
    ):
        if not path.is_file():
            continue
        try:
            _set_dataset(path, _load_frame(path, kind))
            return
        except Exception:
            logger.exception("Не удалось загрузить витрину %s", path)


def _validated_params(intent: str, raw: dict[str, Any]) -> dict[str, Any]:
    model = _MODELS[intent]
    normalised: dict[str, Any] = {}
    for key, value in raw.items():
        if key.startswith("period") and isinstance(value, list):
            value = tuple(value)
        normalised[key] = value
    instance = model(**normalised)
    return instance.model_dump(exclude_none=True)


def _period_for(
    params: dict[str, Any],
    frame: pd.DataFrame,
) -> tuple[PeriodResolution, tuple[str, ...]]:
    if "period1" in params and "period2" in params:
        first = params["period1"]
        second = params["period2"]
        start = min(first[0], second[0])
        end = max(first[1], second[1])
        note = (
            "Сравниваются периоды: "
            f"{format_period(first[0], first[1])} и {format_period(second[0], second[1])}."
        )
        return PeriodResolution(start, end, True), (note,)
    data_min, data_max = _data_bounds(frame)
    return (
        resolve_period(
            params.get("start_date"),
            params.get("end_date"),
            data_min=data_min,
            data_max=data_max,
        ),
        (),
    )


def _format_number(value: float) -> str:
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


def _format_cell(value: Any, *, column: str = "") -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, float):
        if value != value:  # NaN
            return "—"
        return _format_number(value)
    if isinstance(value, (datetime, date)):
        return value.strftime("%d.%m.%Y")
    if isinstance(value, str) and column in ("direction", "status"):
        return _DIRECTION_LABELS.get(value, value)
    return str(value).replace("|", "\\|")


def _render_table(frame: pd.DataFrame) -> tuple[str, int, int]:
    total_rows = len(frame)
    if total_rows == 0:
        return "За выбранный период данных нет.", 0, 0
    view = frame.head(MAX_TABLE_ROWS)
    raw_columns = [str(column) for column in view.columns]
    headers = [_COLUMN_LABELS.get(column, column) for column in raw_columns]
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    for record in view.itertuples(index=False, name=None):
        cells = [
            _format_cell(value, column=raw_columns[index])
            for index, value in enumerate(record)
        ]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines), len(view), total_rows


def _error(text: str, dataset: Dataset | None, period: PeriodResolution | None = None) -> str:
    if dataset is not None:
        data_min, data_max = _data_bounds(dataset.frame)
        resolved = period or PeriodResolution(data_min, data_max, True)
        return format_answer(
            f"Ошибка: {text}",
            AnswerMeta(
                intent="error",
                period=resolved,
                **_as_of_kwargs(dataset.frame),
            ),
        )
    return format_answer(f"Ошибка: {text}", AnswerMeta(intent="error"))


def _short_validation(error: ValidationError) -> str:
    parts: list[str] = []
    for item in error.errors()[:3]:
        location = ".".join(str(part) for part in item.get("loc", ()))
        parts.append(f"{location}: {item.get('msg')}")
    return "; ".join(parts)


def _answer_params(params: dict[str, Any]) -> dict[str, Any]:
    """Параметры среза для тела ответа: только при ``ONEC_MCP_VERBOSE``."""
    if os.environ.get(VERBOSE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}:
        return params
    return {}


def _filter_note(params: dict[str, Any]) -> str:
    """Подтвердить в ответе, что фильтр среза действительно применён."""
    parts: list[str] = []
    if params.get("group_filter"):
        parts.append(f"группа «{params['group_filter']}»")
    if params.get("product_filter"):
        parts.append(f"товар «{params['product_filter']}»")
    if not parts:
        return ""
    return "Фильтр: " + ", ".join(parts) + "."


def _summary_note(
    intent: str,
    params: dict[str, Any],
    frame: pd.DataFrame,
    table: pd.DataFrame,
) -> str:
    """Детерминированные итоги — чтобы агент не складывал строки сам.

    * ``top_bottom_products`` — сумма по показанным строкам и доля в итоге
      периода (без ``group_filter``);
    * ``negative_revenue`` — итог по строкам и их число;
    * ``timeseries`` (agg=sum) — число точек, сумма, минимум и максимум.
    """
    if table.empty:
        return ""
    metric = str(params.get("metric", "revenue"))
    unit = "₽" if metric == "revenue" else "шт"

    if intent == "negative_revenue" and metric in table.columns:
        total = float(table[metric].sum())
        return (
            f"Итого по строкам: {_format_number(total)} {unit}; "
            f"строк: {len(table)}."
        )

    if intent == "timeseries" and metric in table.columns and params.get("agg", "sum") == "sum":
        values = table[metric]
        return (
            f"Точек: {len(table)}; сумма: {_format_number(float(values.sum()))} {unit}; "
            f"минимум: {_format_number(float(values.min()))} {unit}; "
            f"максимум: {_format_number(float(values.max()))} {unit}."
        )

    if intent != "top_bottom_products":
        return ""
    if params.get("group_filter"):
        return ""
    if metric not in table.columns:
        return ""
    shown_sum = float(table[metric].sum())
    total_table = dispatch_intent(
        "total",
        frame,
        {
            "start_date": params.get("start_date"),
            "end_date": params.get("end_date"),
            "product_filter": params.get("product_filter"),
        },
    )
    if total_table.empty:
        return ""
    period_total = float(total_table[metric].iloc[0])
    if period_total == 0:
        return ""
    share = shown_sum / period_total * 100.0
    metric_label = "выручке" if metric == "revenue" else "количеству"
    return (
        f"Сумма по показанным строкам: {_format_number(shown_sum)} {unit}; "
        f"доля в {metric_label} за период: {_format_number(share)} %."
    )


def _dataset_ref(dataset: Dataset | None) -> str | None:
    return _display_path(dataset.path) if dataset is not None else None


def _period_bounds(period: PeriodResolution) -> list[str | None] | None:
    if period.start is None and period.end is None:
        return None
    return [
        period.start.isoformat() if period.start is not None else None,
        period.end.isoformat() if period.end is not None else None,
    ]


def _execute(intent: str, raw_params: dict[str, Any]) -> str:
    dataset = _get_dataset()
    if dataset is None:
        _ensure_default_dataset()
        dataset = _get_dataset()
    if dataset is None:
        _audit.record("refusal", intent=intent, reason="витрина не загружена")
        return format_refusal(
            "витрина не загружена — вызовите load_dataset",
            AVAILABLE,
        )
    try:
        params = _validated_params(intent, raw_params)
    except ValidationError as exc:
        message = f"неверные параметры среза {intent}: {_short_validation(exc)}"
        _audit.record("error", intent=intent, reason="неверные параметры", details=message)
        return _error(message, dataset)
    period, notes = _period_for(params, dataset.frame)
    logger.info("срез %s: %s", intent, params)
    try:
        frame = dispatch_intent(intent, dataset.frame, params)
    except (ValueError, TypeError) as exc:
        message = f"срез {intent} не выполнен: {exc}"
        _audit.record(
            "error",
            intent=intent,
            reason="ошибка движка",
            details=message,
            dataset=_dataset_ref(dataset),
        )
        logger.exception("Срез %s не выполнен", intent)
        return _error(message, dataset, period)
    body, shown, total_rows = _render_table(frame)
    extras = [line for line in (_filter_note(params), _summary_note(intent, params, dataset.frame, frame)) if line]
    if extras:
        body = f"{body}\n\n" + "\n".join(extras)
    _audit.record(
        "slice",
        intent=intent,
        params=params,
        dataset=_dataset_ref(dataset),
        period=_period_bounds(period),
        period_specified=period.specified,
        rows=total_rows,
        shown=shown,
        exported_at=dataset.frame.attrs.get("exported_at"),
        exported_at_source=dataset.frame.attrs.get("exported_at_source"),
    )
    meta = AnswerMeta(
        intent=intent,
        params=_answer_params(params),
        period=period,
        notes=notes,
        shown=shown,
        total=total_rows,
        **_as_of_kwargs(dataset.frame),
    )
    return format_answer(body, meta)


@asynccontextmanager
async def _lifespan(_server: FastMCP):
    """Подгрузить витрину при старте; отсутствие данных не роняет сервер."""
    _ensure_default_dataset()
    yield


mcp = FastMCP(
    "1C-Analyst",
    instructions=(
        "Аналитика продаж из 1С. Числа считает движок: цитируйте их как есть, "
        "не вычисляйте проценты и разности самостоятельно. Если вопрос не "
        "поддерживается, скажите об этом прямо — не подменяйте срез."
    ),
    lifespan=_lifespan,
)


@mcp.tool(annotations=STATEFUL)
def load_dataset(
    path: str | None = None,
    kind: Literal["auto", "sample", "export"] = "auto",
) -> str:
    """Загрузить витрину продаж из CSV внутри проекта.

    Разрешены только файлы внутри репозитория (например,
    ``1c_export/sales_1c.csv`` или ``sample_data.csv``). После загрузки все
    срезы считаются по новой витрине.

    Parameters
    ----------
    path:
        Относительный путь от корня проекта; пусто — витрина по умолчанию.
    kind:
        ``auto`` — определить по колонкам, ``sample`` — синтетика,
        ``export`` — выгрузка из 1С.
    """
    try:
        resolved = _resolve_dataset_path(path, kind)
        frame = _load_frame(resolved, kind)
    except (DatasetError, FileNotFoundError) as exc:
        _audit.record("refusal", intent="load_dataset", reason=str(exc))
        return format_refusal(str(exc), AVAILABLE)
    dataset = _set_dataset(resolved, frame)
    _audit.record(
        "dataset_loaded",
        dataset=_display_path(resolved),
        rows=len(frame),
        exported_at=frame.attrs.get("exported_at"),
        exported_at_source=frame.attrs.get("exported_at_source"),
    )
    return _describe(dataset, intent="load_dataset")


def _describe(dataset: Dataset, *, intent: str) -> str:
    frame = dataset.frame
    data_min, data_max = _data_bounds(frame)
    products = frame["product"].nunique() if "product" in frame.columns else 0
    counterparties = (
        frame["counterparty"].nunique() if "counterparty" in frame.columns else 0
    )
    body = "\n".join(
        [
            f"Витрина: {_display_path(dataset.path)}",
            f"Строк: {len(frame)}",
            f"Товаров: {products}",
            f"Контрагентов: {counterparties}",
            f"Загружено: {dataset.loaded_at.strftime('%d.%m.%Y %H:%M')}",
        ]
    )
    meta = AnswerMeta(
        intent=intent,
        period=PeriodResolution(data_min, data_max, True),
        **_as_of_kwargs(frame),
    )
    return format_answer(body, meta)


@mcp.tool(annotations=READ_ONLY)
def describe_dataset() -> str:
    """Показать текущую витрину: строки, период, дату выгрузки, загрузку."""
    dataset = _get_dataset()
    if dataset is None:
        _ensure_default_dataset()
        dataset = _get_dataset()
    if dataset is None:
        return format_refusal("витрина не загружена — вызовите load_dataset", AVAILABLE)
    return _describe(dataset, intent="describe_dataset")


@mcp.tool(annotations=READ_ONLY)
def describe_capabilities() -> str:
    """Перечислить поддерживаемые срезы и то, чего в v1 нет.

    Используйте перед ответом на вопрос, который может быть вне срезов:
    маржа, дебиторка, остатки и план/факт не поддерживаются.
    """
    lines = ["Поддерживаемые срезы:"]
    lines.extend(f"- {name} — {description}" for name, description in SUPPORTED)
    lines.append(
        "- returns_alert — ежедневный алерт возвратов за вчера "
        "(понедельник — за пятницу–воскресенье; порог настраивается)"
    )
    lines.append(
        "- list_dimensions — имена товарных групп, контрагентов или товаров "
        "для фильтров (только имена, без чисел)"
    )
    lines.append(
        f"- Алерты возвратов: {'включены' if alerts_enabled() else 'отключены'}"
    )
    lines.append("")
    lines.append(f"Не поддерживается: {UNSUPPORTED}.")
    dataset = _get_dataset()
    if dataset is None:
        return format_answer("\n".join(lines), AnswerMeta(intent="capabilities"))
    data_min, data_max = _data_bounds(dataset.frame)
    meta = AnswerMeta(
        intent="capabilities",
        period=PeriodResolution(data_min, data_max, True),
        **_as_of_kwargs(dataset.frame),
    )
    return format_answer("\n".join(lines), meta)


@mcp.tool(annotations=READ_ONLY)
def list_dimensions(
    kind: Literal["groups", "counterparties", "products"] = "groups",
    limit: int = 50,
) -> str:
    """Перечислить значения измерения: группы, контрагенты или товары.

    Только имена, без чисел. Нужен, чтобы не угадывать названия для
    ``group_filter`` / ``product_filter``: взяли имя из списка — передали в
    фильтр среза. ``limit`` ограничивает вывод, в ответе помечается усечение.
    """
    dataset = _get_dataset()
    if dataset is None:
        _ensure_default_dataset()
        dataset = _get_dataset()
    if dataset is None:
        return format_refusal("витрина не загружена — вызовите load_dataset", AVAILABLE)
    columns = {
        "groups": "product_group",
        "counterparties": "counterparty",
        "products": "product",
    }
    column = columns[kind]
    if column not in dataset.frame.columns:
        return _error(f"в витрине нет колонки {column}", dataset)
    values = sorted({str(value) for value in dataset.frame[column].dropna().unique()})
    shown = values[: max(1, limit)]
    body_lines = [f"Значения {kind} (всего {len(values)}):"]
    body_lines.extend(f"- {value}" for value in shown)
    data_min, data_max = _data_bounds(dataset.frame)
    meta = AnswerMeta(
        intent="list_dimensions",
        period=PeriodResolution(data_min, data_max, True),
        shown=len(shown),
        total=len(values),
        **_as_of_kwargs(dataset.frame),
    )
    return format_answer("\n".join(body_lines), meta)


@mcp.tool(annotations=READ_ONLY)
def total(
    start_date: date | None = None,
    end_date: date | None = None,
    product_filter: str | None = None,
) -> str:
    """Итог продаж за период: выручка, количество, число строк.

    Если период не указан, берётся весь доступный период витрины — в ответе
    будет пометка. Пример: ``start_date=2024-01-01, end_date=2024-12-31``.
    """
    return _execute(
        "total",
        {
            "start_date": start_date,
            "end_date": end_date,
            "product_filter": product_filter,
        },
    )


@mcp.tool(annotations=READ_ONLY)
def top_bottom_products(
    start_date: date | None = None,
    end_date: date | None = None,
    metric: Literal["revenue", "quantity"] = "revenue",
    limit: int = 5,
    ascending: bool = False,
    product_filter: str | None = None,
    group_filter: str | None = None,
) -> str:
    """Топ или антитоп товаров за период по выручке или количеству.

    ``ascending=True`` — худшие товары. ``group_filter`` — подстрока названия
    товарной группы. Если период не указан, берётся весь период витрины.
    В конце ответа сервер сам считает сумму по показанным строкам и её долю
    в итоге периода — агенту не нужно складывать и делить.
    """
    return _execute(
        "top_bottom_products",
        {
            "metric": metric,
            "limit": limit,
            "ascending": ascending,
            "start_date": start_date,
            "end_date": end_date,
            "product_filter": product_filter,
            "group_filter": group_filter,
        },
    )


@mcp.tool(annotations=READ_ONLY)
def compare_top_products(
    period1_start: date,
    period1_end: date,
    period2_start: date,
    period2_end: date,
    top_n: int = 5,
    metric: Literal["revenue", "quantity"] = "revenue",
) -> str:
    """Сравнить топ-товары двух периодов (L4L): кто вошёл, выпал, вырос, упал.

    Периоды задаются четырьмя датами; конец включается.
    """
    return _execute(
        "compare_top_products",
        {
            "period1": (period1_start, period1_end),
            "period2": (period2_start, period2_end),
            "top_n": top_n,
            "metric": metric,
        },
    )


@mcp.tool(annotations=READ_ONLY)
def negative_revenue(
    start_date: date | None = None,
    end_date: date | None = None,
    group_by: Literal["product", "counterparty"] = "product",
) -> str:
    """Возвраты и сторно: отрицательная выручка по товарам или контрагентам.

    Это не маржа: витрина не содержит себестоимости. Если период не указан,
    берётся весь период витрины.
    """
    return _execute(
        "negative_revenue",
        {
            "start_date": start_date,
            "end_date": end_date,
            "group_by": group_by,
        },
    )


@mcp.tool(annotations=READ_ONLY)
def returns_alert(
    today: date | None = None,
    min_abs: float | None = None,
    min_rows: int | None = None,
) -> str:
    """Ежедневный алерт возвратов: сумма отрицательной выручки за окно.

    Окно — предыдущий день; в понедельник — пятница, суббота и воскресенье;
    в субботу и воскресенье алерт не отправляется. Порог по умолчанию
    50 000 ₽ и калибруется на истории (``python -m onec_analyst.alerts``).
    Возвращает вердикт, сумму, долю и товары — все числа из движка.
    """
    dataset = _get_dataset()
    if dataset is None:
        _ensure_default_dataset()
        dataset = _get_dataset()
    if dataset is None:
        return format_refusal("витрина не загружена — вызовите load_dataset", AVAILABLE)
    if not alerts_enabled():
        data_min, data_max = _data_bounds(dataset.frame)
        body = (
            "Алерты отключены администратором "
            f"({ALERTS_ENABLED_ENV}=0 или файл {alert_flag_path()}). "
            "Срезы и данные не затронуты; ручной отчёт доступен в веб-приложении."
        )
        return format_answer(
            body,
            AnswerMeta(
                intent="returns_alert",
                period=PeriodResolution(data_min, data_max, True),
                **_as_of_kwargs(dataset.frame),
            ),
        )
    anchor = today or date.today()
    window = alert_window(anchor)
    if window is None:
        data_min, data_max = _data_bounds(dataset.frame)
        body = (
            "Сегодня выходной — алерт не отправляется. "
            "В понедельник придёт сводка за субботу и воскресенье."
        )
        return format_answer(
            body,
            AnswerMeta(
                intent="returns_alert",
                period=PeriodResolution(data_min, data_max, True),
                **_as_of_kwargs(dataset.frame),
            ),
        )
    policy = AlertPolicy.from_env(min_abs=min_abs, min_rows=min_rows)
    stats = collect_returns(dataset.frame, window)
    reason = alert_reason(stats, policy)
    table = dispatch_intent(
        "negative_revenue",
        dataset.frame,
        {
            "start_date": window.start,
            "end_date": window.end,
            "group_by": "product",
        },
    )
    table_text, _shown, _total = _render_table(table)
    window_label = f" {window.label}" if window.label else ""
    lines = [
        f"Возвраты{window_label}: {_format_number(stats.negative_total)} ₽, "
        f"строк: {stats.rows}, доля в выручке: {_format_number(stats.share_pct)} %.",
        f"Порог: {_format_number(policy.min_abs)} ₽, минимум строк: {policy.min_rows}.",
    ]
    if reason is not None:
        lines.append(f"⚠️ Алерт: {reason}.")
        if table.empty:
            lines.append("Строк возвратов нет.")
        else:
            lines.extend(["", table_text])
    else:
        lines.append("Превышения нет — алерт не отправляется.")
    _audit.record(
        "alert",
        window=window.as_dict(),
        amount=stats.negative_total,
        rows=stats.rows,
        triggered=reason is not None,
        dataset=_dataset_ref(dataset),
    )
    return format_answer(
        "\n".join(lines),
        AnswerMeta(
            intent="returns_alert",
            period=PeriodResolution(window.start, window.end, True),
            **_as_of_kwargs(dataset.frame),
        ),
    )


@mcp.tool(annotations=READ_ONLY)
def timeseries(
    start_date: date | None = None,
    end_date: date | None = None,
    freq: Literal["D", "W", "M", "Q", "Y"] = "M",
    metric: Literal["revenue", "quantity"] = "revenue",
    agg: Literal["sum", "mean", "count"] = "sum",
    fill_gaps: bool = True,
    product_filter: str | None = None,
) -> str:
    """Динамика продаж по дням, неделям, месяцам, кварталам или годам."""
    return _execute(
        "timeseries",
        {
            "freq": freq,
            "metric": metric,
            "agg": agg,
            "start_date": start_date,
            "end_date": end_date,
            "fill_gaps": fill_gaps,
            "product_filter": product_filter,
        },
    )


@mcp.tool(annotations=READ_ONLY)
def counterparty_dynamics(
    period1_start: date,
    period1_end: date,
    period2_start: date,
    period2_end: date,
    metric: Literal["revenue", "quantity"] = "revenue",
    top_n: int | None = None,
    min_abs_delta: float | None = None,
    sort_by: Literal["delta", "delta_abs", "period1_value", "period2_value", "name"] = "delta_abs",
    only_drops: bool = False,
    min_base_revenue: float | None = None,
) -> str:
    """Рост или падение по контрагентам в двух периодах.

    ``only_drops=True`` — только падения; ``min_base_revenue`` отсекает мелких
    контрагентов, чтобы не шуметь.
    """
    return _execute(
        "counterparty_dynamics",
        {
            "period1": (period1_start, period1_end),
            "period2": (period2_start, period2_end),
            "metric": metric,
            "top_n": top_n,
            "min_abs_delta": min_abs_delta,
            "sort_by": sort_by,
            "only_drops": only_drops,
            "min_base_revenue": min_base_revenue,
        },
    )


@mcp.tool(annotations=READ_ONLY)
def group_dynamics(
    period1_start: date,
    period1_end: date,
    period2_start: date,
    period2_end: date,
    group_by: Literal["product_group", "product", "counterparty"] = "product_group",
    metric: Literal["revenue", "quantity"] = "revenue",
    top_n: int | None = None,
    min_abs_delta: float | None = None,
    sort_by: Literal["delta", "delta_abs", "period1_value", "period2_value", "name"] = "delta_abs",
    only_drops: bool = False,
    group_filter: str | None = None,
) -> str:
    """Рост или падение по товарным группам в двух периодах.

    ``only_drops=True`` — только просевшие группы; ``group_filter`` — подстрока
    названия группы.
    """
    return _execute(
        "group_dynamics",
        {
            "period1": (period1_start, period1_end),
            "period2": (period2_start, period2_end),
            "group_by": group_by,
            "metric": metric,
            "top_n": top_n,
            "min_abs_delta": min_abs_delta,
            "sort_by": sort_by,
            "only_drops": only_drops,
            "group_filter": group_filter,
        },
    )


def main() -> None:
    """Запустить сервер по stdio; логи пишутся только в stderr."""
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
