"""Хранилище выгрузки 1С в SQLite: номенклатура и продажи отдельным файлом.

Зачем отдельная БД, а не ``sessions.db``:

* журнал сессий — append-only лог запросов («одна строка = одна сессия») со своей
  политикой хранения и выгрузкой в Notion; данные 1С живут по другим правилам;
* смешивание ломает метрики журнала (``COUNT(*)``, «ждут выгрузки») и делает
  бэкап/ротацию бессмысленными — у данных и лога разный жизненный цикл.

Поэтому выгрузка кладётся в ``db/analytics.db`` (каталог ``db/`` в ``.gitignore``):
таблицы ``nomenclature`` и ``sales``, служебная ``meta`` с источником, временем,
sha256 и найденными аномалиями. Загрузка идемпотентна: файл читается целиком и
перезаписывается в одной транзакции, поэтому повторный запуск не плодит строк.

Модуль не участвует в обработке вопроса: приложение по-прежнему читает CSV.
Это архив/SQL-слой, а подключение его как источника аналитики — отдельный шаг.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from onec_analyst.paths import find_repo_root

__all__ = [
    "ANALYTICS_DB_ENV",
    "DEFAULT_ANALYTICS_DB",
    "DEFAULT_NOMENCLATURE_CSV",
    "DEFAULT_SALES_CSV",
    "LoadResult",
    "connect",
    "load_nomenclature",
    "load_sales",
    "quality_report",
    "refresh",
    "resolve_db_path",
    "stats",
]

logger = logging.getLogger("onec_analyst.analytics_db")

#: Переменная окружения для пути к БД (тесты и развёртывания).
ANALYTICS_DB_ENV = "ONEC_ANALYTICS_DB"

#: Путь по умолчанию — рядом с журналом сессий, но отдельным файлом.
DEFAULT_ANALYTICS_DB = Path("db") / "analytics.db"

#: Пути выгрузки 1С по умолчанию (относительно корня проекта).
DEFAULT_NOMENCLATURE_CSV = Path("1c_export") / "nomenclature_1c_v2.csv"
DEFAULT_SALES_CSV = Path("1c_export") / "sales_1c_v2.csv"

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS nomenclature (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        parent TEXT,
        is_group INTEGER NOT NULL DEFAULT 0,
        product_group TEXT,
        product_group_path TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_nomenclature_name ON nomenclature(name)",
    """
    CREATE TABLE IF NOT EXISTS sales (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        product TEXT,
        product_group TEXT,
        product_group_path TEXT,
        counterparty TEXT,
        quantity REAL,
        revenue_net REAL,
        revenue_gross REAL,
        operation TEXT,
        registrar TEXT,
        organization TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sales_date ON sales(date)",
    "CREATE INDEX IF NOT EXISTS idx_sales_product ON sales(product)",
    "CREATE INDEX IF NOT EXISTS idx_sales_counterparty ON sales(counterparty)",
    "CREATE INDEX IF NOT EXISTS idx_sales_group ON sales(product_group)",
    """
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    # --- Тестовые артефакты: программа, результаты, golden-кассеты ---------
    """
    CREATE TABLE IF NOT EXISTS test_program (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        test_order INTEGER,
        test_id TEXT,
        question TEXT,
        predecessor TEXT,
        intent TEXT,
        note TEXT,
        source_file TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_test_program_id ON test_program(test_id)",
    """
    CREATE TABLE IF NOT EXISTS test_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        model TEXT NOT NULL,
        test_order INTEGER,
        test_id TEXT,
        question TEXT,
        status TEXT,
        expected_status TEXT,
        intent TEXT,
        expected_intent TEXT,
        dsl_match INTEGER,
        fact_match TEXT,
        verdict INTEGER,
        seconds REAL,
        today TEXT,
        source_file TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_test_results_model ON test_results(model)",
    "CREATE INDEX IF NOT EXISTS idx_test_results_id ON test_results(test_id)",
    """
    CREATE TABLE IF NOT EXISTS test_summary (
        model TEXT PRIMARY KEY,
        cases INTEGER,
        passed INTEGER,
        percent REAL,
        status_ok INTEGER,
        dsl_ok INTEGER,
        facts_ok INTEGER,
        errors INTEGER,
        p50_sec REAL,
        source_file TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS golden_cassettes (
        case_id TEXT PRIMARY KEY,
        source TEXT,
        recorded_at TEXT,
        provider TEXT,
        model TEXT,
        payload TEXT NOT NULL,
        sha256 TEXT,
        file_path TEXT,
        loaded_at TEXT
    )
    """,
)

#: Колонки продаж в порядке таблицы.
_SALES_COLUMNS = (
    "date",
    "product",
    "product_group",
    "product_group_path",
    "counterparty",
    "quantity",
    "revenue_net",
    "revenue_gross",
    "operation",
    "registrar",
    "organization",
)

#: Колонки номенклатуры в порядке таблицы.
_NOMENCLATURE_COLUMNS = (
    "name",
    "parent",
    "is_group",
    "product_group",
    "product_group_path",
)


@dataclass(frozen=True)
class LoadResult:
    """Итог загрузки: строки, источник и найденные аномалии."""

    nomenclature_rows: int
    sales_rows: int
    date_min: str | None
    date_max: str | None
    anomalies: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "nomenclature_rows": self.nomenclature_rows,
            "sales_rows": self.sales_rows,
            "date_min": self.date_min,
            "date_max": self.date_max,
            "anomalies": self.anomalies,
        }


def resolve_db_path() -> Path:
    """Путь к БД: ``ONEC_ANALYTICS_DB`` или ``db/analytics.db`` в корне проекта."""
    raw = os.environ.get(ANALYTICS_DB_ENV, "").strip()
    return Path(raw) if raw else find_repo_root() / DEFAULT_ANALYTICS_DB


def connect(db_path: str | os.PathLike[str] | None = None) -> sqlite3.Connection:
    """Открыть БД и создать схему (идемпотентно); WAL — для чтения при записи."""
    target = Path(db_path) if db_path is not None else resolve_db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(target, timeout=5.0)
    for statement in _SCHEMA:
        connection.execute(statement)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:  # сетевой диск/только чтение — не повод падать
        logger.warning("Не удалось включить WAL для %s", target)
    return connection


def _read_csv(path: Path, required: Iterable[str]) -> list[dict[str, str]]:
    """Прочитать CSV (UTF-8, возможен BOM) и проверить обязательные колонки."""
    if not path.is_file():
        raise FileNotFoundError(f"Файл выгрузки не найден: {path}")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        header = [name.strip() for name in (reader.fieldnames or [])]
        missing = [name for name in required if name not in header]
        if missing:
            raise ValueError(f"{path}: нет колонок {missing}; есть {header}")
        return [dict(row) for row in reader]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _as_float(value: Any) -> float | None:
    """Число из CSV: пусто/мусор → ``None`` (не подменяем нулём молча)."""
    text = str(value or "").strip().replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _as_int(value: Any) -> int:
    try:
        return int(float(str(value or "0").strip() or 0))
    except ValueError:
        return 0


def _meta_set(connection: sqlite3.Connection, key: str, value: Any) -> None:
    connection.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value),
    )


def _meta_all(connection: sqlite3.Connection) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in connection.execute("SELECT key, value FROM meta"):
        try:
            result[key] = json.loads(value)
        except (TypeError, ValueError):
            result[key] = value
    return result


def load_nomenclature(connection: sqlite3.Connection, path: Path) -> int:
    """Полностью перезаписать ``nomenclature`` из CSV; вернуть число строк."""
    rows = _read_csv(path, _NOMENCLATURE_COLUMNS)
    connection.execute("DELETE FROM nomenclature")
    connection.executemany(
        f"INSERT INTO nomenclature ({', '.join(_NOMENCLATURE_COLUMNS)}) "
        f"VALUES ({', '.join('?' for _ in _NOMENCLATURE_COLUMNS)})",
        [
            (
                str(row.get("name") or "").strip(),
                str(row.get("parent") or "").strip() or None,
                _as_int(row.get("is_group")),
                str(row.get("product_group") or "").strip() or None,
                str(row.get("product_group_path") or "").strip() or None,
            )
            for row in rows
        ],
    )
    _meta_set(connection, "nomenclature_source", str(path))
    _meta_set(connection, "nomenclature_sha256", _sha256(path))
    _meta_set(
        connection, "nomenclature_loaded_at", datetime.now().astimezone().isoformat(timespec="seconds")
    )
    _meta_set(connection, "nomenclature_rows", len(rows))
    return len(rows)


def load_sales(connection: sqlite3.Connection, path: Path) -> int:
    """Полностью перезаписать ``sales`` из CSV; вернуть число строк."""
    rows = _read_csv(path, _SALES_COLUMNS)
    connection.execute("DELETE FROM sales")
    connection.executemany(
        f"INSERT INTO sales ({', '.join(_SALES_COLUMNS)}) "
        f"VALUES ({', '.join('?' for _ in _SALES_COLUMNS)})",
        [
            (
                str(row.get("date") or "").strip(),
                str(row.get("product") or "").strip() or None,
                str(row.get("product_group") or "").strip() or None,
                str(row.get("product_group_path") or "").strip() or None,
                str(row.get("counterparty") or "").strip() or None,
                _as_float(row.get("quantity")),
                _as_float(row.get("revenue_net")),
                _as_float(row.get("revenue_gross")),
                str(row.get("operation") or "").strip() or None,
                str(row.get("registrar") or "").strip() or None,
                str(row.get("organization") or "").strip() or None,
            )
            for row in rows
        ],
    )
    _meta_set(connection, "sales_source", str(path))
    _meta_set(connection, "sales_sha256", _sha256(path))
    _meta_set(
        connection, "sales_loaded_at", datetime.now().astimezone().isoformat(timespec="seconds")
    )
    _meta_set(connection, "sales_rows", len(rows))
    return len(rows)


def quality_report(
    rows: list[dict[str, str]],
    nomenclature: list[dict[str, str]],
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """Сводка аномалий выгрузки — без правки данных.

    Ничего не фильтруем при загрузке: архив должен совпадать с выгрузкой. Но
    аномалии фиксируем в ``meta``, чтобы агрегаты потом не строились вслепую.
    """
    today_iso = (today or date.today()).isoformat()
    dates = sorted(row.get("date", "") for row in rows if row.get("date"))
    duplicate_names = sorted(
        {
            name
            for name in {row.get("name", "") for row in nomenclature}
            if sum(1 for row in nomenclature if row.get("name") == name) > 1
        }
    )
    return {
        "rows": len(rows),
        "date_min": dates[0] if dates else None,
        "date_max": dates[-1] if dates else None,
        "future_dates": sum(1 for row in rows if str(row.get("date", "")) > today_iso),
        "zero_revenue": sum(1 for row in rows if _as_float(row.get("revenue_net")) == 0),
        "negative_revenue": sum(
            1 for row in rows if (_as_float(row.get("revenue_net")) or 0) < 0
        ),
        "zero_quantity": sum(1 for row in rows if _as_float(row.get("quantity")) == 0),
        "transfers": sum(
            1 for row in rows if "передача" in str(row.get("operation", "")).lower()
        ),
        "operations": sorted({str(row.get("operation") or "") for row in rows}),
        "organizations": sorted({str(row.get("organization") or "") for row in rows}),
        "duplicate_nomenclature_names": duplicate_names,
    }


def refresh(
    *,
    nomenclature_csv: Path,
    sales_csv: Path,
    db_path: str | os.PathLike[str] | None = None,
    today: date | None = None,
) -> LoadResult:
    """Полная перезагрузка БД из двух CSV в одной транзакции.

    Идемпотентно: повторный запуск заменяет данные, а не добавляет новые строки.
    """
    rows = _read_csv(sales_csv, _SALES_COLUMNS)
    nomenclature = _read_csv(nomenclature_csv, _NOMENCLATURE_COLUMNS)
    anomalies = quality_report(rows, nomenclature, today=today)

    connection = connect(db_path)
    try:
        with connection:
            nomenclature_rows = load_nomenclature(connection, nomenclature_csv)
            sales_rows = load_sales(connection, sales_csv)
            _meta_set(connection, "anomalies", anomalies)
    finally:
        connection.close()
    return LoadResult(
        nomenclature_rows=nomenclature_rows,
        sales_rows=sales_rows,
        date_min=anomalies["date_min"],
        date_max=anomalies["date_max"],
        anomalies=anomalies,
    )


def stats(db_path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Что лежит в БД: счётчики, источники, время загрузки, аномалии."""
    target = Path(db_path) if db_path is not None else resolve_db_path()
    if not target.exists():
        return {"db": str(target), "exists": False}
    connection = sqlite3.connect(target, timeout=5.0)
    try:
        meta = _meta_all(connection)
        counts = {
            "nomenclature": connection.execute("SELECT COUNT(*) FROM nomenclature").fetchone()[0],
            "sales": connection.execute("SELECT COUNT(*) FROM sales").fetchone()[0],
        }
    finally:
        connection.close()
    return {"db": str(target), "exists": True, "counts": counts, "meta": meta}
