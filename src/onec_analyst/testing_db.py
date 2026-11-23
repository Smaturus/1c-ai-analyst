"""Тестовые артефакты в `analytics.db`: программа, результаты, golden-кассеты.

Это не данные 1С, а история проверок: какие кейсы задавались, чем закончились у
каждой модели, и как выглядели записанные ответы. Хранится в той же БД, что и
выгрузка, но **отдельными таблицами** — перезагрузка продаж их не трогает
(``refresh()`` удаляет только ``nomenclature``/``sales``/``meta``).

Загрузка идемпотентна: каждый импорт полностью перезаписывает свои таблицы.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from onec_analyst.analytics_db import connect, resolve_db_path

__all__ = [
    "DEFAULT_CASSETTES_DIR",
    "import_testing",
    "load_cassettes",
    "load_program",
    "load_results",
    "stats",
    "testing_db_path",
]

logger = logging.getLogger("onec_analyst.testing_db")

#: Каталог golden-кассет по умолчанию (в репозитории).
DEFAULT_CASSETTES_DIR = Path("tests") / "golden" / "cassettes"

#: Колонки листа «Сводка» в результатах тестирования.
_SUMMARY_COLUMNS = (
    "model",
    "cases",
    "passed",
    "percent",
    "status_ok",
    "dsl_ok",
    "facts_ok",
    "errors",
    "p50_sec",
)

#: Колонки листа модели в результатах тестирования.
_RESULT_COLUMNS = (
    "order",
    "id",
    "question",
    "status",
    "expected_status",
    "intent",
    "expected_intent",
    "dsl_match",
    "fact_match",
    "verdict",
    "seconds",
    "today",
)


def testing_db_path() -> Path:
    """Путь к БД с тестовыми артефактами (та же ``analytics.db``)."""
    return resolve_db_path()


def _as_int(value: Any) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return int(float(str(value).replace(",", ".")))
    except ValueError:
        return None


def _as_float(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return None


def _as_bool_int(value: Any) -> int | None:
    """``True/False`` (в т.ч. строками) → 1/0, пусто → ``None``."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"", "none", "null"}:
        return None
    if text in {"true", "1", "yes", "да"}:
        return 1
    if text in {"false", "0", "no", "нет"}:
        return 0
    return None


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _meta_set(connection: sqlite3.Connection, key: str, value: Any) -> None:
    connection.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value, ensure_ascii=False)),
    )


def load_program(connection: sqlite3.Connection, path: Path) -> int:
    """Перезаписать ``test_program`` из книги «Программа тестирования»."""
    if not path.is_file():
        raise FileNotFoundError(f"Файл программы тестирования не найден: {path}")
    book = load_workbook(path, read_only=True, data_only=True)
    try:
        rows = [
            list(row) for row in book.worksheets[0].iter_rows(values_only=True)
        ]
    finally:
        book.close()

    body = rows[1:] if rows else []
    values = []
    for row in body:
        padded = list(row) + [None] * (6 - len(row))
        order, test_id, question, predecessor, intent, note = padded[:6]
        if _as_text(test_id) is None and _as_text(question) is None:
            continue
        values.append(
            (
                _as_int(order),
                _as_text(test_id),
                _as_text(question),
                _as_text(predecessor),
                _as_text(intent),
                _as_text(note),
                str(path),
            )
        )

    connection.execute("DELETE FROM test_program")
    connection.executemany(
        "INSERT INTO test_program "
        "(test_order, test_id, question, predecessor, intent, note, source_file) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        values,
    )
    _meta_set(connection, "test_program_source", str(path))
    _meta_set(connection, "test_program_sha256", _sha256(path))
    _meta_set(connection, "test_program_loaded_at", _now())
    _meta_set(connection, "test_program_rows", len(values))
    return len(values)


def load_results(connection: sqlite3.Connection, path: Path) -> tuple[int, int]:
    """Перезаписать ``test_summary`` и ``test_results`` из книги результатов.

    Возвращает ``(строк сводки, строк результатов)``.
    """
    if not path.is_file():
        raise FileNotFoundError(f"Файл результатов тестирования не найден: {path}")
    book = load_workbook(path, read_only=True, data_only=True)
    try:
        summary_rows = [
            list(row) for row in book["Сводка"].iter_rows(values_only=True)
        ]
        model_sheets = {
            sheet.title: [list(row) for row in sheet.iter_rows(values_only=True)]
            for sheet in book.worksheets
            if sheet.title != "Сводка"
        }
    finally:
        book.close()

    summary_values = []
    for row in summary_rows[1:] if summary_rows else []:
        padded = list(row) + [None] * (len(_SUMMARY_COLUMNS) - len(row))
        model = _as_text(padded[0])
        if model is None:
            continue
        summary_values.append(
            (
                model,
                _as_int(padded[1]),
                _as_int(padded[2]),
                _as_float(padded[3]),
                _as_int(padded[4]),
                _as_int(padded[5]),
                _as_int(padded[6]),
                _as_int(padded[7]),
                _as_float(padded[8]),
                str(path),
            )
        )

    result_values = []
    for model, rows in model_sheets.items():
        for row in rows[1:] if rows else []:
            padded = list(row) + [None] * (len(_RESULT_COLUMNS) - len(row))
            if _as_text(padded[1]) is None and _as_text(padded[2]) is None:
                continue
            result_values.append(
                (
                    model,
                    _as_int(padded[0]),
                    _as_text(padded[1]),
                    _as_text(padded[2]),
                    _as_text(padded[3]),
                    _as_text(padded[4]),
                    _as_text(padded[5]),
                    _as_text(padded[6]),
                    _as_bool_int(padded[7]),
                    _as_text(padded[8]),
                    _as_bool_int(padded[9]),
                    _as_float(padded[10]),
                    _as_text(padded[11]),
                    str(path),
                )
            )

    connection.execute("DELETE FROM test_summary")
    connection.execute("DELETE FROM test_results")
    connection.executemany(
        "INSERT INTO test_summary "
        "(model, cases, passed, percent, status_ok, dsl_ok, facts_ok, errors, p50_sec, source_file) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        summary_values,
    )
    connection.executemany(
        "INSERT INTO test_results "
        "(model, test_order, test_id, question, status, expected_status, intent, "
        "expected_intent, dsl_match, fact_match, verdict, seconds, today, source_file) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        result_values,
    )
    _meta_set(connection, "test_results_source", str(path))
    _meta_set(connection, "test_results_sha256", _sha256(path))
    _meta_set(connection, "test_results_loaded_at", _now())
    _meta_set(connection, "test_results_rows", len(result_values))
    return len(summary_values), len(result_values)


def load_cassettes(
    connection: sqlite3.Connection, paths: Iterable[Path]
) -> int:
    """Перезаписать ``golden_cassettes``: разобранные поля + сырой JSON."""
    values = []
    loaded_at = _now()
    for path in sorted(Path(item) for item in paths):
        if not path.is_file():
            raise FileNotFoundError(f"Кассета не найдена: {path}")
        raw = path.read_text(encoding="utf-8")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}: невалидный JSON ({exc})") from exc
        if not isinstance(payload, dict) or not payload.get("case_id"):
            raise ValueError(f"{path}: нет поля case_id")
        values.append(
            (
                str(payload["case_id"]),
                _as_text(payload.get("source")),
                _as_text(payload.get("recorded_at")),
                _as_text(payload.get("provider")),
                _as_text(payload.get("model")),
                raw,
                hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                str(path),
                loaded_at,
            )
        )

    connection.execute("DELETE FROM golden_cassettes")
    connection.executemany(
        "INSERT INTO golden_cassettes "
        "(case_id, source, recorded_at, provider, model, payload, sha256, file_path, loaded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        values,
    )
    _meta_set(connection, "golden_cassettes_loaded_at", loaded_at)
    _meta_set(connection, "golden_cassettes_rows", len(values))
    return len(values)


def import_testing(
    *,
    program_xlsx: Path | None = None,
    results_xlsx: Path | None = None,
    cassettes: Iterable[Path] | None = None,
    cassettes_dir: Path | None = None,
    db_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Загрузить в БД то, что передано: книгу программы, книгу результатов, кассеты.

    Любой источник можно опустить — тогда соответствующая таблица не трогается.
    Импорт идемпотентен: повторный запуск перезаписывает данные.
    """
    if cassettes is None and cassettes_dir is not None:
        cassettes = sorted(Path(cassettes_dir).glob("test_*.json"))
    if not any([program_xlsx, results_xlsx, cassettes]):
        raise ValueError("Не передано ни одного источника для импорта")

    connection = connect(db_path)
    stats_result: dict[str, Any] = {"db": str(db_path or resolve_db_path())}
    try:
        with connection:
            if program_xlsx is not None:
                stats_result["program_rows"] = load_program(connection, Path(program_xlsx))
            if results_xlsx is not None:
                summary_rows, result_rows = load_results(connection, Path(results_xlsx))
                stats_result["summary_rows"] = summary_rows
                stats_result["result_rows"] = result_rows
            if cassettes:
                stats_result["cassette_rows"] = load_cassettes(
                    connection, [Path(item) for item in cassettes]
                )
    finally:
        connection.close()
    return stats_result


def stats(db_path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Что лежит в тестовых таблицах (для CLI и диагностики)."""
    target = Path(db_path) if db_path is not None else resolve_db_path()
    if not target.exists():
        return {"db": str(target), "exists": False}
    connection = sqlite3.connect(target, timeout=5.0)
    try:
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "test_program",
                "test_results",
                "test_summary",
                "golden_cassettes",
            )
        }
        models = [
            row[0]
            for row in connection.execute(
                "SELECT model FROM test_summary ORDER BY model"
            )
        ]
    finally:
        connection.close()
    return {"db": str(target), "exists": True, "counts": counts, "models": models}
