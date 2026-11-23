#!/usr/bin/env python3
"""Экспорт журнала аналитических сессий в Notion (вне пути обработки запроса).

Запускается вручную (``scripts/notion_export.py``) или кнопкой «Выгрузить» в UI —
она выгружает только сессии активного диалога. Из пути обработки вопроса Notion
не вызывается никогда.

Что попадает наружу:

* только метаданные сессии (дата, провайдер, модель, статус, вердикт,
  длительность, токены, деградация) и сводка результата;
* текст вопроса **никогда** не отправляется — вместо него плейсхолдер
  ``<PROMPT>``. Строки, похожие на ПДн (e-mail, телефон, ИНН), заменяются на
  ``<PII>``.

Свойства Notion-базы (создаются через ``--create-database``):

* ``Название`` — title, «Сессия + дата»;
* ``Дата`` — date;
* ``Провайдер`` — select;
* ``Статус`` — select: ``ok`` / ``clarify`` / ``refuse`` / ``error``;
* ``Verdict`` — checkbox;
* ``Длительность`` — number (секунды);
* ``Токены`` — number (сейчас всегда пусто, см. ``session_log``);
* ``Degraded`` — checkbox.

В теле страницы — блок «Результат» (текст сводки + таблица) и PNG-график,
загруженный через File Upload API; локальная копия markdown и PNG остаётся
в ``exports/``.

Секреты — только из окружения: ``NOTION_API_KEY`` и ``NOTION_DATABASE_ID``.
Обработка 429: экспоненциальный backoff с приоритетом заголовка ``Retry-After``.

Примеры::

    python scripts/notion_export.py --dry-run
    python scripts/notion_export.py --limit 20
    python scripts/notion_export.py --chat <chat_id>
    python scripts/notion_export.py --create-database --parent-page <page_id>
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from onec_analyst.session_log import (
    mark_exported,
    pending_records,
)

#: Корень репозитория: нужен для каталога ``exports/`` по умолчанию.
_REPO_ROOT = Path(__file__).resolve().parents[2]

logger = logging.getLogger("notion_export")

#: Плейсхолдеры санитизации: наружу уходит только их текст.
PROMPT_PLACEHOLDER = "<PROMPT>"
PII_PLACEHOLDER = "<PII>"

#: Имена свойств базы — один контракт для ``--create-database`` и экспорта.
PROP_TITLE = "Название"
PROP_DATE = "Дата"
PROP_PROVIDER = "Провайдер"
PROP_STATUS = "Статус"
PROP_VERDICT = "Verdict"
PROP_LATENCY = "Длительность"
PROP_TOKENS = "Токены"
PROP_DEGRADED = "Degraded"
PROP_ANSWER = "Ответ модели"

#: Значения select «Статус» (совпадают с журналом; ``fallback`` уже свёрнут).
STATUS_OPTIONS = ("ok", "clarify", "refuse", "error")

#: Шаблоны ПДн: e-mail, телефоны и ИНН/ОГРН-подобные числа.
#: Даты сюда не попадают намеренно — они уходят в свойства как метаданные.
_PII_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), PII_PLACEHOLDER),
    (re.compile(r"(?<!\d)\+\d[\d\s()\-]{8,}\d(?!\d)"), PII_PLACEHOLDER),
    (
        re.compile(
            r"(?<!\d)8[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}(?!\d)"
        ),
        PII_PLACEHOLDER,
    ),
    (re.compile(r"(?<!\d)\d{10}(?!\d)"), PII_PLACEHOLDER),
    (re.compile(r"(?<!\d)\d{12}(?!\d)"), PII_PLACEHOLDER),
)

#: Параметры backoff по умолчанию (секунды).
BACKOFF_BASE_SEC = 1.0
BACKOFF_MAX_SEC = 60.0
MAX_ATTEMPTS = 5

#: Задержка перед следующим вызовом (подменяется в тестах без реального сна).
_sleep: Callable[[float], None] = time.sleep


# --------------------------------------------------------------------------- #
# Санитизация
# --------------------------------------------------------------------------- #
def sanitize_text(value: Any) -> str:
    """Заменить ПДн-подобные фрагменты плейсхолдером.

    Текст вопроса сюда не попадает по построению: у него отдельный режим
    :func:`question_placeholder`. Функция нужна как страховка для любых
    свободных полей (провайдер, модель, имя файла).
    """
    text = "" if value is None else str(value)
    for pattern, replacement in _PII_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def question_placeholder() -> str:
    """Текст вопроса для внешней системы: наружу уходит только плейсхолдер."""
    return PROMPT_PLACEHOLDER


def clip(value: Any, limit: int = 2000) -> str:
    """Обрезать строку до лимита Notion (rich_text ≤ 2000 символов)."""
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --------------------------------------------------------------------------- #
# 429 / Retry-After
# --------------------------------------------------------------------------- #
def _retry_after_seconds(error: BaseException) -> float | None:
    """Достать ``Retry-After`` из ошибки (httpx.Headers или dict)."""
    headers = getattr(error, "headers", None)
    if not headers:
        return None
    try:
        raw = headers.get("Retry-After") or headers.get("retry-after")
    except Exception:
        return None
    if raw is None:
        return None
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return max(0.0, value)


def _is_rate_limited(error: BaseException) -> bool:
    """429 — единственная ошибка, которую повторяем (по статусу, не по классу SDK)."""
    return getattr(error, "status", None) == 429


def call_with_backoff(
    call: Callable[[], Any],
    *,
    max_attempts: int = MAX_ATTEMPTS,
    base_delay: float = BACKOFF_BASE_SEC,
    max_delay: float = BACKOFF_MAX_SEC,
) -> Any:
    """Вызвать API, повторяя 429 с exponential backoff.

    Приоритет задержки — ``Retry-After`` (если сервер его прислал), иначе
    ``base_delay * 2**attempt`` с потолком ``max_delay``. Остальные ошибки
    пробрасываются сразу: молча ретраить 401/400 нельзя.
    """
    for attempt in range(max(1, int(max_attempts))):
        try:
            return call()
        except Exception as exc:
            if not _is_rate_limited(exc) or attempt >= max_attempts - 1:
                raise
            delay = _retry_after_seconds(exc)
            if delay is None:
                delay = min(base_delay * (2**attempt), max_delay)
            delay = min(delay, max_delay)
            logger.warning(
                "Notion 429: повтор через %.1f с (попытка %d из %d)",
                delay,
                attempt + 1,
                max_attempts,
            )
            _sleep(delay)
    raise RuntimeError("backoff: недостижимая ветка")  # pragma: no cover


# --------------------------------------------------------------------------- #
# Notion: свойства, блоки страницы, загрузка файла
# --------------------------------------------------------------------------- #
def _rich_text(text: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": {"content": clip(sanitize_text(text))}}]


def build_properties(row: dict[str, Any]) -> dict[str, Any]:
    """Свойства страницы: название+дата, провайдер, статус, вердикт, метрики."""
    ts = str(row.get("ts") or datetime.now().astimezone().isoformat(timespec="seconds"))
    status = str(row.get("status") or "error")
    if status not in STATUS_OPTIONS:
        status = "error"
    provider = sanitize_text(row.get("provider") or "").strip()
    title = f"Сессия {ts[:10]} · {status}"

    properties: dict[str, Any] = {
        PROP_TITLE: {"title": _rich_text(title)},
        PROP_DATE: {"date": {"start": ts}},
        PROP_STATUS: {"select": {"name": status}},
        PROP_VERDICT: {"checkbox": bool(row.get("verdict"))},
        PROP_DEGRADED: {"checkbox": bool(row.get("degraded"))},
    }
    if provider:
        properties[PROP_PROVIDER] = {"select": {"name": clip(provider, 100)}}
    latency = row.get("latency_sec")
    if latency is not None:
        properties[PROP_LATENCY] = {"number": round(float(latency), 3)}
    tokens = _tokens_total(row)
    if tokens is not None:
        properties[PROP_TOKENS] = {"number": tokens}
    model_answer = model_answer_json(row)
    if model_answer:
        properties[PROP_ANSWER] = {"rich_text": _rich_text(model_answer)}
    return properties


def model_answer_json(row: dict[str, Any]) -> str | None:
    """DSL решения компактным JSON — то, что вернула модель (или правила).

    Формат: ``{"intent": ..., "params": {...}}``. Для ``clarification`` остаются
    только ``question`` и ``missing``: список ``suggestions`` — это UI-подсказки,
    а не ответ модели, и в свойстве они выглядели как «приклеенные» вопросы.
    Если DSL нет (старые строки) — ``None``, свойство не отправляется.
    """
    raw = row.get("dsl")
    if not raw:
        return None
    try:
        payload = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or not payload:
        return None
    payload = {key: value for key, value in payload.items() if key != "suggestions"}
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def _chart_needed(row: dict[str, Any]) -> bool:
    """Нужен ли график на странице: только для успешной сессии с результатом.

    У ``clarify``/``refuse``/``error`` расчёта нет — там график показывал бы
    длительность в один столбик, что выглядит как случайная картинка.
    """
    return str(row.get("status") or "") == "ok"


def _tokens_total(row: dict[str, Any]) -> int | None:
    """Сумма токенов, если журнал их знает (сейчас всегда ``None``)."""
    prompt = row.get("prompt_tokens")
    output = row.get("output_tokens")
    if prompt is None and output is None:
        return None
    return int(prompt or 0) + int(output or 0)


def summary_rows(row: dict[str, Any]) -> list[tuple[str, str]]:
    """Строки таблицы в теле страницы — только метаданные, без текста вопроса."""
    tokens = _tokens_total(row)
    return [
        ("Сессия", sanitize_text(row.get("session_id") or "—")),
        ("Дата", str(row.get("ts") or "—")),
        ("Вопрос", question_placeholder()),
        ("Провайдер", sanitize_text(row.get("provider") or "—")),
        ("Модель", sanitize_text(row.get("model") or "—")),
        ("Статус", str(row.get("status") or "—")),
        ("Verdict", "да" if row.get("verdict") else "нет"),
        ("Длительность, с", _fmt(row.get("latency_sec"))),
        ("Токены", "—" if tokens is None else str(tokens)),
        ("Degraded", "да" if row.get("degraded") else "нет"),
        ("Интент DSL", _dsl_intent(row)),
    ]


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.3f}".replace(".", ",")
    except (TypeError, ValueError):
        return str(value)


def _dsl_intent(row: dict[str, Any]) -> str:
    """Интент из DSL (JSON-текст) — без параметров и сущностей."""
    raw = row.get("dsl")
    if not raw:
        return "—"
    try:
        payload = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (ValueError, TypeError):
        return "—"
    intent = payload.get("intent") if isinstance(payload, dict) else None
    return sanitize_text(intent or "—")


def _num(value: Any) -> str:
    """Число для таблицы результата: разряды пробелами, два знака после запятой."""
    if value is None:
        return "—"
    try:
        return f"{float(value):,.2f}".replace(",", " ").replace(".", ",")
    except (TypeError, ValueError):
        return str(value)


def _summary(row: dict[str, Any]) -> dict[str, Any]:
    """Разобрать сохранённую сводку результата (JSON-текст) в dict.

    Пустая/битая сводка — не ошибка: страница просто останется без блока
    «Результат» (так выгружаются уточнения и отказы, где считать нечего).
    """
    raw = row.get("summary")
    if not raw:
        return {}
    try:
        payload = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


#: Подписи внутренних колонок DataFrame результата для таблицы в отчёте.
_RESULT_COLUMN_LABELS = {
    "rank": "№",
    "product": "Товар",
    "counterparty": "Контрагент",
    "product_group": "Товарная группа",
    "revenue": "Выручка без НДС, ₽",
    "quantity": "Количество, шт",
    "share_pct": "Доля, %",
    "delta": "Δ",
    "delta_abs": "Δ, ₽",
    "delta_pct": "Δ, %",
    "direction": "Направление",
    "period": "Период",
    "period_start": "Начало периода",
    "period_end": "Конец периода",
    "period1": "Период 1",
    "period2": "Период 2",
    "period1_value": "Значение (1)",
    "period2_value": "Значение (2)",
    "name": "Название",
    "rank_p1": "Место (1)",
    "rank_p2": "Место (2)",
    "rows_count": "Строк",
    "status": "Статус",
    "check": "Проверка",
    "value": "Значение",
    "item": "Показатель",
}

#: Приоритет колонок-значений для графика (что рисуем столбиками).
_CHART_VALUE_COLUMNS = ("revenue", "quantity", "delta_abs", "share_pct", "value")
#: Приоритет колонок-подписей для графика.
_CHART_LABEL_COLUMNS = ("product", "counterparty", "product_group", "item", "period")


def _cell(value: Any) -> str:
    """Значение ячейки результата: числа по-русски, пустое — прочерк."""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if abs(value) >= 1000:
            return f"{value:,.2f}".replace(",", " ").replace(".", ",")
        if value.is_integer():
            return str(int(value))
        return f"{value:.2f}".replace(".", ",")
    return sanitize_text(value)


def result_rows(row: dict[str, Any]) -> tuple[str, list[tuple[str, ...]]]:
    """Текст сводки и таблица результата из сохранённого снимка DataFrame.

    Основной источник — ``summary["table"]`` (то, что видел пользователь).
    Если снимка нет (старые записи), падаем на ``summary["values"]``.
    """
    summary = _summary(row)
    if not summary:
        return "", []
    text = sanitize_text(summary.get("text") or "")
    table = summary.get("table") or {}
    columns = [str(column) for column in (table.get("columns") or [])]
    rows = table.get("rows") or []
    if columns and rows:
        header = tuple(_RESULT_COLUMN_LABELS.get(column, column) for column in columns)
        body = [tuple(_cell(value) for value in record) for record in rows]
        return text, [header, *body]
    values: list[tuple[str, ...]] = []
    for value in summary.get("values") or ():
        if not isinstance(value, dict):
            continue
        values.append(
            (
                sanitize_text(value.get("item") or "—"),
                sanitize_text(value.get("period") or "—"),
                sanitize_text(value.get("metric") or "—"),
                _cell(value.get("value")),
            )
        )
    if not values:
        return text, []
    return text, [("Показатель", "Период", "Метрика", "Значение"), *values]


def _chart_pairs(row: dict[str, Any]) -> list[tuple[str, float]]:
    """Пары «подпись → значение» для графика: сначала результат, потом метрики."""
    summary = _summary(row)
    values = [
        value
        for value in (summary.get("values") or ())
        if isinstance(value, dict) and value.get("value") is not None
    ]
    if len(values) >= 2:
        return [
            (str(value.get("item") or "—"), float(value["value"]))
            for value in values
        ]
    table = summary.get("table") or {}
    columns = [str(column) for column in (table.get("columns") or [])]
    rows = table.get("rows") or []
    if not columns or not rows:
        return []
    value_index = next(
        (columns.index(name) for name in _CHART_VALUE_COLUMNS if name in columns),
        None,
    )
    label_index = next(
        (columns.index(name) for name in _CHART_LABEL_COLUMNS if name in columns),
        None,
    )
    if value_index is None or label_index is None:
        return []
    pairs: list[tuple[str, float]] = []
    for record in rows:
        if len(record) <= max(value_index, label_index):
            continue
        amount = record[value_index]
        if isinstance(amount, (int, float)) and not isinstance(amount, bool):
            pairs.append((str(record[label_index]), float(amount)))
    return pairs


def _table_block(
    rows: Sequence[Sequence[str]], width: int
) -> dict[str, Any]:
    """Нативный table-блок Notion из готовых строк."""
    return {
        "object": "block",
        "type": "table",
        "table": {
            "table_width": width,
            "has_column_header": True,
            "has_row_header": False,
            "children": [
                {
                    "object": "block",
                    "type": "table_row",
                    "table_row": {
                        "cells": [_rich_text(cell) for cell in row]
                    },
                }
                for row in rows
            ],
        },
    }


def build_page_blocks(
    row: dict[str, Any],
    *,
    chart_upload_id: str | None,
) -> list[dict[str, Any]]:
    """Тело страницы: результат анализа, метаданные сессии и PNG.

    Пути к локальным файлам в страницу не попадают: они имеют смысл только на
    машине, где лежит журнал, и для читателя Notion выглядят как мусор.
    """
    blocks: list[dict[str, Any]] = []

    text, values = result_rows(row)
    if text or values:
        blocks.append(
            {
                "object": "block",
                "type": "heading_2",
                "heading_2": {"rich_text": _rich_text("Результат")},
            }
        )
        if text:
            blocks.append(
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {"rich_text": _rich_text(text)},
                }
            )
        if values:
            blocks.append(_table_block(values, len(values[0])))

    blocks.extend(
        [
            {
                "object": "block",
                "type": "heading_2",
                "heading_2": {"rich_text": _rich_text("Сессия")},
            },
            _table_block(summary_rows(row), 2),
        ]
    )
    if chart_upload_id:
        blocks.append(
            {
                "object": "block",
                "type": "image",
                "image": {
                    "type": "file_upload",
                    "file_upload": {"id": chart_upload_id},
                },
            }
        )
    return blocks


def upload_png(client: Any, png: bytes, filename: str) -> str:
    """Загрузить PNG через File Upload API и вернуть ``file_upload.id``.

    Для ``single_part`` хватает двух шагов: создать upload и отправить файл —
    ``send`` сам переводит объект в статус ``uploaded``. ``complete`` нужен только
    для ``multi_part`` (там файл остаётся ``pending`` до сборки частей); вызов
    ``complete`` после ``single_part`` Notion отклоняет с 400.
    """
    created = call_with_backoff(
        lambda: client.file_uploads.create(
            mode="single_part",
            filename=filename,
            content_type="image/png",
        )
    )
    upload_id = str(created["id"])
    call_with_backoff(
        lambda: client.file_uploads.send(
            file_upload_id=upload_id,
            file=(filename, png, "image/png"),
        )
    )
    return upload_id


# --------------------------------------------------------------------------- #
# График и локальная копия
# --------------------------------------------------------------------------- #
#: Человекочитаемые подписи метрик для оси графика.
_METRIC_LABELS = {"revenue": "Выручка, ₽", "quantity": "Количество, шт"}


def _metric_label(metric: str) -> str:
    return _METRIC_LABELS.get(metric, metric or "Значение")


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_chart(rows: list[dict[str, Any]]) -> bytes:
    """PNG-график (matplotlib, backend Agg — без окна).

    Приоритет — значения результата: для топа это столбики по выручке, что и
    показывает страница. Если сводки нет (уточнение/отказ), рисуем длительность
    сессий — единственную числовую метрику, которая есть всегда.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    items: list[str] = []
    amounts: list[float] = []
    metric = ""
    period = ""
    for row in rows:
        pairs = _chart_pairs(row)
        summary = _summary(row)
        for value in summary.get("values") or ():
            if isinstance(value, dict):
                metric = str(value.get("metric") or metric)
                period = str(value.get("period") or period)
        for label, amount in pairs:
            items.append(label)
            amounts.append(amount)

    figure, axes = plt.subplots(figsize=(7.2, 3.4), dpi=150)
    if items:
        pairs = sorted(
            zip(items, amounts, strict=False),
            key=lambda pair: abs(pair[1]),
            reverse=True,
        )[:10]
        labels = [_truncate(label, 28) for label, _ in pairs]
        values = [value for _, value in pairs]
        positions = list(range(len(values)))
        axes.barh(positions, values, color="#4c78a8")
        axes.set_yticks(positions)
        axes.set_yticklabels(labels)
        axes.invert_yaxis()
        axes.set_xlabel(_metric_label(metric))
        title = "Результат анализа" + (f" · {period}" if period else "")
        axes.set_title(title)
    else:
        labels = [str(index + 1) for index in range(len(rows))]
        positions = list(range(len(rows)))
        values = [float(row.get("latency_sec") or 0.0) for row in rows]
        colors = [
            "#4c78a8" if str(row.get("status")) == "ok" else "#e45756" for row in rows
        ]
        axes.bar(positions, values, color=colors)
        axes.set_xticks(positions)
        axes.set_xticklabels(labels)
        axes.set_title("Длительность сессий, с")
        axes.set_xlabel("Сессия в выгрузке")
        axes.set_ylabel("Секунды")
    figure.tight_layout()
    from io import BytesIO

    buffer = BytesIO()
    figure.savefig(buffer, format="png")
    plt.close(figure)
    return buffer.getvalue()


def write_local_export(
    row: dict[str, Any],
    out_dir: Path,
    *,
    chart_png: bytes | None = None,
) -> tuple[Path, Path | None]:
    """Сохранить markdown-копию (и PNG рядом); вернуть пути."""
    out_dir.mkdir(parents=True, exist_ok=True)
    session = str(row.get("session_id") or "session")[:8]
    stamp = str(row.get("ts") or "now")[:10]
    markdown_path = out_dir / f"session_{stamp}_{session}.md"
    text, values = result_rows(row)
    lines = [f"# Сессия {stamp} · {session}", ""]
    if text:
        lines += ["## Результат", "", text, ""]
    if values:
        lines.append("| " + " | ".join(values[0]) + " |")
        lines.append("|" + "---|" * len(values[0]))
        lines += ["| " + " | ".join(row_values) + " |" for row_values in values[1:]]
        lines.append("")
    lines += ["## Сессия", "", "| Поле | Значение |", "|---|---|"]
    lines += [f"| {label} | {value} |" for label, value in summary_rows(row)]
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    png_path: Path | None = None
    if chart_png is not None:
        png_path = out_dir / f"session_{stamp}_{session}.png"
        png_path.write_bytes(chart_png)
        with markdown_path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n![График]({png_path.name})\n")
    return markdown_path, png_path


# --------------------------------------------------------------------------- #
# Создание базы (одноразовая операция вместе с владельцем workspace)
# --------------------------------------------------------------------------- #
def database_schema() -> dict[str, Any]:
    """Схема свойств Notion-базы: 9 свойств из ТЗ, без таблицы в свойствах."""
    return {
        PROP_TITLE: {"title": {}},
        PROP_DATE: {"date": {}},
        PROP_PROVIDER: {
            "select": {
                "options": [
                    {"name": name}
                    for name in ("deepseek", "gigachat", "ollama", "rules")
                ]
            }
        },
        PROP_STATUS: {
            "select": {"options": [{"name": name} for name in STATUS_OPTIONS]}
        },
        PROP_VERDICT: {"checkbox": {}},
        PROP_LATENCY: {"number": {"format": "number"}},
        PROP_TOKENS: {"number": {"format": "number"}},
        PROP_DEGRADED: {"checkbox": {}},
        PROP_ANSWER: {"rich_text": {}},
    }


def ensure_properties(client: Any, database_id: str) -> list[str]:
    """Добавить недостающие свойства в уже созданную базу; вернуть их имена.

    Нужно после появления новых полей (например, «Ответ модели»): базы, созданные
    раньше, не получают их автоматически. Существующие свойства не меняются.
    """
    parent = resolve_parent(client, database_id)
    data_source_id = parent.get("data_source_id")
    if not data_source_id:
        raise ValueError(
            "База старого формата без data source: добавьте свойство вручную"
        )
    current = client.data_sources.retrieve(data_source_id=data_source_id)["properties"]
    wanted = database_schema()
    missing = {name: schema for name, schema in wanted.items() if name not in current}
    if not missing:
        return []
    call_with_backoff(
        lambda: client.data_sources.update(
            data_source_id=data_source_id, properties=missing
        )
    )
    return sorted(missing)


#: Статусы беклога (свойство select).
BACKLOG_STATUSES = ("Идея", "Беклог", "В работе", "Готово")
#: Приоритеты беклога.
BACKLOG_PRIORITIES = ("Низкий", "Средний", "Высокий")
#: Типы задач беклога.
BACKLOG_KINDS = ("Функция", "Дефект", "Техдолг", "Исследование")


def backlog_schema() -> dict[str, Any]:
    """Схема базы беклога: задача, статус, приоритет, тип, спринт, критерии.

    ``ID`` — автонумерация Notion (``unique_id``): стабильное «задача N» для
    ссылок в чате и коммитах. ``Критерий готовности`` — без него карточка через
    месяц нечитаема, поэтому поле есть сразу, а не «потом добавим».
    """
    return {
        "Название": {"title": {}},
        "ID": {"unique_id": {"prefix": "TASK"}},
        "Статус": {
            "select": {"options": [{"name": name} for name in BACKLOG_STATUSES]}
        },
        "Приоритет": {
            "select": {"options": [{"name": name} for name in BACKLOG_PRIORITIES]}
        },
        "Тип": {"select": {"options": [{"name": name} for name in BACKLOG_KINDS]}},
        "Спринт": {"number": {"format": "number"}},
        "Дата": {"date": {}},
        "Описание": {"rich_text": {}},
        "Критерий готовности": {"rich_text": {}},
        "Ссылка": {"url": {}},
        "Источник": {"rich_text": {}},
    }


def ensure_backlog_properties(client: Any, database_id: str) -> list[str]:
    """Добавить в существующую базу беклога недостающие свойства (не трогая старые)."""
    parent = resolve_parent(client, database_id)
    data_source_id = parent.get("data_source_id")
    if not data_source_id:
        raise ValueError("База беклога без data source: добавьте свойства вручную")
    current = client.data_sources.retrieve(data_source_id=data_source_id)["properties"]
    wanted = backlog_schema()
    missing = {name: schema for name, schema in wanted.items() if name not in current}
    if not missing:
        return []
    call_with_backoff(
        lambda: client.data_sources.update(
            data_source_id=data_source_id, properties=missing
        )
    )
    return sorted(missing)


def create_backlog_database(
    client: Any, parent_page_id: str, title: str = "Беклог"
) -> tuple[str, str]:
    """Создать базу беклога под страницей-родителем; вернуть ``(database_id, data_source_id)``."""
    created = call_with_backoff(
        lambda: client.databases.create(
            parent={"type": "page_id", "page_id": parent_page_id},
            title=_rich_text(title),
            initial_data_source={"properties": backlog_schema()},
        )
    )
    database_id = str(created["id"])
    sources = created.get("data_sources") or []
    data_source_id = str(sources[0]["id"]) if sources else database_id
    return database_id, data_source_id


def _normalize_title(text: Any) -> str:
    """Ключ сравнения названий: без регистра, лишних пробелов и ё→е."""
    value = " ".join(str(text or "").casefold().replace("ё", "е").split())
    return value


class BacklogItemExists(RuntimeError):
    """В беклоге уже есть карточка с таким названием.

    Атрибуты ``page_id`` и ``title`` позволяют вызвать код показать, что нашёл,
    вместо создания дубля.
    """

    def __init__(self, title: str, page_id: str) -> None:
        super().__init__(f"В беклоге уже есть задача «{title}» ({page_id})")
        self.title = title
        self.page_id = page_id


def find_backlog_item(client: Any, database_id: str, title: str) -> str | None:
    """Найти карточку беклога по названию (регистронезависимо); вернуть ``page_id``.

    Notion не умеет полнотекстовый поиск по заголовку через API, поэтому
    вычитываем страницы базы (первая сотня — для беклога это запас) и сравниваем
    нормализованные названия.
    """
    parent = resolve_parent(client, database_id)
    data_source_id = parent.get("data_source_id")
    if not data_source_id:
        return None
    key = _normalize_title(title)
    if not key:
        return None
    results = call_with_backoff(
        lambda: client.data_sources.query(data_source_id=data_source_id, page_size=100)
    ).get("results", [])
    for page in results:
        properties = page.get("properties") or {}
        title_value = properties.get("Название", {}).get("title") or []
        plain = "".join(part.get("plain_text", "") for part in title_value)
        if _normalize_title(plain) == key:
            return str(page.get("id"))
    return None


def add_backlog_item(
    client: Any,
    database_id: str,
    *,
    title: str,
    description: str = "",
    status: str = "Беклог",
    priority: str = "Средний",
    kind: str = "Функция",
    source: str = "",
    day: str | None = None,
    sprint: int | None = None,
    acceptance: str = "",
    url: str = "",
    allow_duplicate: bool = False,
) -> str:
    """Добавить карточку в беклог; вернуть ``page_id``.

    ``database_id`` может быть id базы или data source — родитель резолвится так
    же, как при выгрузке сессий. По умолчанию дубль по названию не создаётся:
    бросается :class:`BacklogItemExists`, чтобы поток «я говорю — ты заносишь»
    не плодил одинаковые карточки.

    Parameters
    ----------
    sprint:
        Номер спринта (``Спринт``, number). ``None`` — «не в спринте».
    acceptance:
        Критерий готовности: как поймём, что задача сделана.
    url:
        Ссылка на обсуждение/файл/PR (``Ссылка``, url).
    allow_duplicate:
        Осознанно разрешить дубль (например, переоткрытая задача).
    """
    if not allow_duplicate:
        existing = find_backlog_item(client, database_id, title)
        if existing is not None:
            raise BacklogItemExists(title, existing)
    parent = resolve_parent(client, database_id)
    properties: dict[str, Any] = {
        "Название": {"title": _rich_text(title)},
        "Статус": {"select": {"name": status}},
        "Приоритет": {"select": {"name": priority}},
        "Тип": {"select": {"name": kind}},
    }
    if description:
        properties["Описание"] = {"rich_text": _rich_text(description)}
    if source:
        properties["Источник"] = {"rich_text": _rich_text(source)}
    if day:
        properties["Дата"] = {"date": {"start": day}}
    if sprint is not None:
        properties["Спринт"] = {"number": int(sprint)}
    if acceptance:
        properties["Критерий готовности"] = {"rich_text": _rich_text(acceptance)}
    if url:
        properties["Ссылка"] = {"url": url}
    page = call_with_backoff(
        lambda: client.pages.create(parent=parent, properties=properties)
    )
    return str(page["id"])


def create_database(client: Any, parent_page_id: str, title: str) -> tuple[str, str]:
    """Создать базу под страницей-родителем; вернуть ``(database_id, data_source_id)``.

    Начиная с версии API ``2025-09-03`` база и её ``data source`` — разные
    объекты: свойства задаются в ``initial_data_source``, а страницы создаются
    под ``data_source_id``. Поэтому возвращаем оба id.
    """
    created = call_with_backoff(
        lambda: client.databases.create(
            parent={"type": "page_id", "page_id": parent_page_id},
            title=_rich_text(title),
            initial_data_source={"properties": database_schema()},
        )
    )
    database_id = str(created["id"])
    sources = created.get("data_sources") or []
    data_source_id = str(sources[0]["id"]) if sources else database_id
    return database_id, data_source_id


def resolve_parent(client: Any, database_or_data_source_id: str) -> dict[str, Any]:
    """Собрать ``parent`` для страницы: ``data_source_id`` или старый ``database_id``.

    Принимает и id базы, и id data source: если база не читается (404/400),
    считаем аргумент уже id источника. Для базы без ``data_sources`` (API до
    2025-09-03) остаётся ``database_id``.
    """
    try:
        database = call_with_backoff(
            lambda: client.databases.retrieve(
                database_id=database_or_data_source_id
            )
        )
    except Exception as exc:
        if getattr(exc, "status", None) in (400, 404):
            return {
                "type": "data_source_id",
                "data_source_id": database_or_data_source_id,
            }
        raise
    sources = database.get("data_sources") or []
    if sources:
        return {"type": "data_source_id", "data_source_id": str(sources[0]["id"])}
    return {"type": "database_id", "database_id": database_or_data_source_id}


# --------------------------------------------------------------------------- #
# Основной проход
# --------------------------------------------------------------------------- #
def export_pending(
    *,
    client: Any,
    database_id: str,
    db_path: str | os.PathLike[str] | None = None,
    limit: int = 50,
    out_dir: Path | None = None,
    dry_run: bool = False,
    chat_id: str | None = None,
    chart_builder: Callable[[list[dict[str, Any]]], bytes] = build_chart,
) -> dict[str, Any]:
    """Выгрузить невыгруженные сессии; вернуть счётчики прохода.

    ``chat_id`` ограничивает выгрузку одним диалогом (кнопка «Выгрузить»);
    ``None`` — все невыгруженные строки. Уже выгруженные строки не попадают в
    выборку никогда (``exported_at IS NOT NULL``), поэтому повторные нажатия
    дублей не создают.

    Ошибка на одной записи не останавливает пачку: страница не создана — строка
    остаётся невыгруженной и попадёт в следующий запуск.
    """
    rows = pending_records(db_path, limit=limit, chat_id=chat_id)
    stats: dict[str, Any] = {"pending": len(rows), "exported": 0, "failed": 0}
    if not rows:
        return stats

    # Родитель страницы резолвится один раз на пачку: id базы → data_source_id
    # (API 2025-09-03) или database_id для базы старого формата.
    parent = (
        {"type": "data_source_id", "data_source_id": database_id}
        if dry_run
        else resolve_parent(client, database_id)
    )
    for row in rows:
        try:
            if dry_run:
                stats["exported"] += 1
                logger.info(
                    "dry-run: сессия %s, статус %s, вопрос %s",
                    row.get("session_id"),
                    row.get("status"),
                    question_placeholder(),
                )
                continue
            # График — свой на каждую страницу и только для успешных сессий: у
            # clarify/refuse/error результата нет, и столбик длительности там
            # выглядит как случайная картинка. Локальная копия пишется всегда,
            # но визуального шума в Notion не добавляет.
            png = chart_builder([row]) if _chart_needed(row) else None
            write_local_export(
                row, out_dir or (_REPO_ROOT / "exports"), chart_png=png
            )
            upload_id = (
                upload_png(
                    client, png, f"session_{str(row.get('session_id'))[:8]}.png"
                )
                if png
                else None
            )

            def create_page(
                current: dict[str, Any] = row, chart_id: str | None = upload_id
            ) -> Any:
                return client.pages.create(
                    parent=parent,
                    properties=build_properties(current),
                    children=build_page_blocks(current, chart_upload_id=chart_id),
                )

            page = call_with_backoff(create_page)
            mark_exported(db_path, int(row["id"]), str(page["id"]))
            stats["exported"] += 1
        except Exception:
            stats["failed"] += 1
            logger.exception("Не удалось выгрузить сессию %s", row.get("session_id"))
    return stats


def make_client(token: str) -> Any:
    """Создать Notion-клиент; SDK импортируется лениво (тесты без пакета)."""
    from notion_client import Client

    # Собственный backoff вместо встроенного: он должен быть виден в логах и
    # тестах, а не спрятан в SDK.
    return Client(auth=token, retry=False)


class ExportNotConfigured(RuntimeError):
    """Выгрузка невозможна: нет ключа или id базы в окружении."""


def export_chat(
    chat_id: str,
    *,
    db_path: str | os.PathLike[str] | None = None,
    limit: int = 50,
    out_dir: Path | None = None,
    client: Any = None,
    token: str | None = None,
    database_id: str | None = None,
) -> dict[str, Any]:
    """Выгрузить невыгруженные сессии активного диалога (кнопка «Выгрузить»).

    Возвращает счётчики :func:`export_pending`: ``pending``/``exported``/``failed``.
    Уже выгруженные сессии этого диалога в выборку не попадают, поэтому повторное
    нажатие даёт ``pending: 0`` и новых страниц не создаёт.

    Raises
    ------
    ExportNotConfigured
        Если в окружении нет ``NOTION_API_KEY`` или ``NOTION_DATABASE_ID``.
    """
    token = (token if token is not None else _env("NOTION_API_KEY")).strip()
    database_id = (
        database_id if database_id is not None else _env("NOTION_DATABASE_ID")
    ).strip()
    if not token:
        raise ExportNotConfigured("Не задан NOTION_API_KEY")
    if not database_id:
        raise ExportNotConfigured("Не задан NOTION_DATABASE_ID")
    return export_pending(
        client=client or make_client(token),
        database_id=database_id,
        db_path=db_path,
        limit=limit,
        out_dir=out_dir,
        chat_id=chat_id,
    )


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def main(argv: list[str] | None = None) -> int:
    """CLI: экспорт новых сессий, dry-run или создание базы."""
    parser = argparse.ArgumentParser(description="Экспорт журнала сессий в Notion")
    parser.add_argument("--limit", type=int, default=50, help="сколько записей за проход")
    parser.add_argument("--db", default=None, help="путь к sessions.db (по умолчанию db/sessions.db)")
    parser.add_argument("--out", default=None, help="каталог локальных копий (по умолчанию exports/)")
    parser.add_argument("--dry-run", action="store_true", help="ничего не отправлять в Notion")
    parser.add_argument(
        "--chat",
        default=None,
        help="выгрузить только сессии одного диалога (chat_id из журнала)",
    )
    parser.add_argument("--create-database", action="store_true", help="создать Notion-базу под страницей")
    parser.add_argument(
        "--ensure-properties",
        action="store_true",
        help="добавить недостающие свойства в существующую базу",
    )
    parser.add_argument(
        "--create-backlog",
        action="store_true",
        help="создать базу беклога под страницей (вместе с --parent-page)",
    )
    parser.add_argument(
        "--ensure-backlog",
        action="store_true",
        help="добавить недостающие свойства в существующую базу беклога",
    )
    parser.add_argument("--backlog-id", default=None, help="id базы беклога")
    parser.add_argument("--add-task", default=None, help="название задачи для беклога")
    parser.add_argument("--task-kind", default="Функция", help="тип задачи беклога")
    parser.add_argument("--task-priority", default="Средний", help="приоритет задачи")
    parser.add_argument("--task-status", default="Беклог", help="статус задачи")
    parser.add_argument("--task-description", default="", help="описание задачи")
    parser.add_argument("--task-acceptance", default="", help="критерий готовности")
    parser.add_argument("--task-sprint", type=int, default=None, help="номер спринта")
    parser.add_argument("--task-url", default="", help="ссылка к задаче")
    parser.add_argument("--task-source", default="", help="источник задачи")
    parser.add_argument("--parent-page", default=None, help="id страницы-родителя для --create-database")
    parser.add_argument("--title", default="Аналитические сессии", help="заголовок создаваемой базы")
    parser.add_argument("--verbose", action="store_true", help="подробный лог")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.dry_run and not (args.create_database or args.create_backlog):
        stats = export_pending(
            client=None,
            database_id="dry-run",
            db_path=args.db,
            limit=args.limit,
            out_dir=Path(args.out) if args.out else None,
            dry_run=True,
            chat_id=args.chat,
        )
        print(json.dumps(stats, ensure_ascii=False))
        return 0

    token = _env("NOTION_API_KEY")
    if not token:
        logger.error("Не задан NOTION_API_KEY — экспорт невозможен")
        return 2
    client = make_client(token)

    if args.create_database:
        if not args.parent_page:
            logger.error("Для --create-database нужен --parent-page <page_id>")
            return 2
        database_id, data_source_id = create_database(
            client, args.parent_page, args.title
        )
        print(json.dumps({"database_id": database_id, "data_source_id": data_source_id}))
        logger.info(
            "База создана. В окружение: NOTION_DATABASE_ID=%s (data source: %s)",
            database_id,
            data_source_id,
        )
        return 0

    if args.create_backlog:
        if not args.parent_page:
            logger.error("Для --create-backlog нужен --parent-page <page_id>")
            return 2
        database_id, data_source_id = create_backlog_database(
            client, args.parent_page, args.title
        )
        print(json.dumps({"database_id": database_id, "data_source_id": data_source_id}))
        logger.info(
            "База беклога создана. В окружение: NOTION_BACKLOG_DATABASE_ID=%s (data source: %s)",
            database_id,
            data_source_id,
        )
        return 0

    backlog_id = (args.backlog_id or _env("NOTION_BACKLOG_DATABASE_ID")).strip()
    if args.ensure_backlog or args.add_task:
        result: dict[str, Any] = {}
        if not backlog_id:
            logger.error(
                "Не задан id базы беклога: --backlog-id или NOTION_BACKLOG_DATABASE_ID"
            )
            return 2
        if args.ensure_backlog:
            result["added"] = ensure_backlog_properties(client, backlog_id)
        if args.add_task:
            try:
                page_id = add_backlog_item(
                    client,
                    backlog_id,
                    title=args.add_task,
                    description=args.task_description,
                    status=args.task_status,
                    priority=args.task_priority,
                    kind=args.task_kind,
                    source=args.task_source,
                    sprint=args.task_sprint,
                    acceptance=args.task_acceptance,
                    url=args.task_url,
                )
            except BacklogItemExists as exc:
                print(json.dumps({"existing": exc.page_id, "title": exc.title}, ensure_ascii=False))
                logger.error("%s", exc)
                return 3
            result["page_id"] = page_id
        print(json.dumps(result, ensure_ascii=False))
        return 0

    database_id = _env("NOTION_DATABASE_ID")
    if not database_id:
        logger.error("Не задан NOTION_DATABASE_ID — укажи id базы (создать: --create-database)")
        return 2

    if args.ensure_properties:
        added = ensure_properties(client, database_id)
        print(json.dumps({"added": added}, ensure_ascii=False))
        return 0

    stats = export_pending(
        client=client,
        database_id=database_id,
        db_path=args.db,
        limit=args.limit,
        out_dir=Path(args.out) if args.out else None,
        chat_id=args.chat,
    )
    print(json.dumps(stats, ensure_ascii=False))
    return 0 if stats["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
