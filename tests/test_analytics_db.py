"""Загрузка выгрузки 1С в отдельную SQLite-БД: схема, идемпотентность, аномалии."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from onec_analyst import analytics_db
from onec_analyst.analytics_db import (
    ANALYTICS_DB_ENV,
    connect,
    refresh,
    resolve_db_path,
    stats,
)

NOMENCLATURE_HEADER = "name,parent,is_group,product_group,product_group_path"
SALES_HEADER = (
    "date,product,product_group,product_group_path,counterparty,quantity,"
    "revenue_net,revenue_gross,operation,registrar,organization"
)


@pytest.fixture()
def csv_files(tmp_path: Path) -> tuple[Path, Path]:
    """Маленькая выгрузка: дубликат имени, будущая дата, нули, возврат, передача."""
    nomenclature = tmp_path / "nomenclature.csv"
    nomenclature.write_text(
        "\n".join(
            [
                NOMENCLATURE_HEADER,
                'P1,,"0",G1,Group / G1',
                'P2,,"0",G1,Group / G1',
                'Чехол Eta,,"1",G2,Group / G2',
                'Чехол Eta,,"0",G2,Group / G2',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    sales = tmp_path / "sales.csv"
    sales.write_text(
        "\n".join(
            [
                SALES_HEADER,
                "2024-01-15,P1,G1,Group / G1,A,10,1000.0,1200.0,Реализация,R-1,ООО 1",
                "2024-02-15,P2,G1,Group / G1,B,0,0.0,0.0,Отчет комиссионера,R-2,ООО 1",
                "2024-03-15,P1,G1,Group / G1,A,-2,-500.0,-600.0,Реализация,R-3,ООО 2",
                "2099-01-01,P2,G1,Group / G1,C,1,100.0,120.0,Передача товаров,R-4,ООО 2",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return nomenclature, sales


def _counts(db_path: Path) -> dict[str, int]:
    connection = sqlite3.connect(db_path)
    try:
        return {
            "nomenclature": connection.execute(
                "SELECT COUNT(*) FROM nomenclature"
            ).fetchone()[0],
            "sales": connection.execute("SELECT COUNT(*) FROM sales").fetchone()[0],
        }
    finally:
        connection.close()


def test_refresh_creates_tables_and_rows(
    csv_files: tuple[Path, Path], tmp_path: Path
) -> None:
    """Данные из двух CSV попадают в свои таблицы."""
    nomenclature, sales = csv_files
    db_path = tmp_path / "analytics.db"
    result = refresh(nomenclature_csv=nomenclature, sales_csv=sales, db_path=db_path)

    assert _counts(db_path) == {"nomenclature": 4, "sales": 4}
    assert result.nomenclature_rows == 4
    assert result.sales_rows == 4
    assert result.date_min == "2024-01-15"
    assert result.date_max == "2099-01-01"


def test_types_are_numeric(csv_files: tuple[Path, Path], tmp_path: Path) -> None:
    """Количество и выручка — числа, ``is_group`` — 0/1, дата — ISO-текст."""
    nomenclature, sales = csv_files
    db_path = tmp_path / "analytics.db"
    refresh(nomenclature_csv=nomenclature, sales_csv=sales, db_path=db_path)

    connection = sqlite3.connect(db_path)
    try:
        quantity, revenue = connection.execute(
            "SELECT quantity, revenue_net FROM sales ORDER BY id LIMIT 1"
        ).fetchone()
        is_group = connection.execute(
            "SELECT is_group FROM nomenclature WHERE name = 'Чехол Eta' ORDER BY id LIMIT 1"
        ).fetchone()[0]
        date_value = connection.execute("SELECT date FROM sales ORDER BY id LIMIT 1").fetchone()[0]
    finally:
        connection.close()

    assert isinstance(quantity, float) and quantity == 10.0
    assert revenue == 1000.0
    assert is_group == 1
    assert date_value == "2024-01-15"


def test_refresh_is_idempotent(csv_files: tuple[Path, Path], tmp_path: Path) -> None:
    """Повторная загрузка заменяет данные, а не добавляет дубли."""
    nomenclature, sales = csv_files
    db_path = tmp_path / "analytics.db"
    refresh(nomenclature_csv=nomenclature, sales_csv=sales, db_path=db_path)
    refresh(nomenclature_csv=nomenclature, sales_csv=sales, db_path=db_path)
    assert _counts(db_path) == {"nomenclature": 4, "sales": 4}


def test_anomalies_are_recorded(csv_files: tuple[Path, Path], tmp_path: Path) -> None:
    """Аномалии считаются и остаются в ``meta``: ничего не фильтруем молча."""
    nomenclature, sales = csv_files
    db_path = tmp_path / "analytics.db"
    result = refresh(
        nomenclature_csv=nomenclature,
        sales_csv=sales,
        db_path=db_path,
        today=date(2024, 12, 31),
    )
    anomalies = result.anomalies
    assert anomalies["future_dates"] == 1
    assert anomalies["zero_revenue"] == 1
    assert anomalies["zero_quantity"] == 1
    assert anomalies["negative_revenue"] == 1
    assert anomalies["transfers"] == 1
    assert anomalies["duplicate_nomenclature_names"] == ["Чехол Eta"]

    stored = stats(db_path)["meta"]["anomalies"]
    assert stored["future_dates"] == 1


def test_missing_file_raises(tmp_path: Path) -> None:
    """Отсутствующий CSV — понятная ошибка, а не пустая БД."""
    with pytest.raises(FileNotFoundError):
        refresh(
            nomenclature_csv=tmp_path / "nope.csv",
            sales_csv=tmp_path / "nope2.csv",
            db_path=tmp_path / "analytics.db",
        )


def test_wrong_header_raises(tmp_path: Path) -> None:
    """Несовпадение схемы выгрузки не должно тихо испортить архив."""
    bad = tmp_path / "bad.csv"
    bad.write_text("date,product\n2024-01-01,P1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        refresh(
            nomenclature_csv=bad,
            sales_csv=bad,
            db_path=tmp_path / "analytics.db",
        )


def test_stats_for_missing_db(tmp_path: Path) -> None:
    """``--stats`` на пустом месте сообщает, что базы нет, и не создаёт файл."""
    missing = tmp_path / "absent.db"
    assert stats(missing)["exists"] is False
    assert not missing.exists()


def test_resolve_db_path_uses_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Путь можно переопределить переменной окружения (как и остальные БД)."""
    target = tmp_path / "custom.db"
    monkeypatch.setenv(ANALYTICS_DB_ENV, str(target))
    assert resolve_db_path() == target


def test_connect_is_idempotent(tmp_path: Path) -> None:
    """Повторный ``connect`` не ломает схему и не теряет данные."""
    db_path = tmp_path / "analytics.db"
    connection = connect(db_path)
    try:
        connection.execute(
            "INSERT INTO meta (key, value) VALUES ('k', 'v')"
        )
        connection.commit()
    finally:
        connection.close()
    connection = connect(db_path)
    try:
        value = connection.execute("SELECT value FROM meta WHERE key = 'k'").fetchone()[0]
    finally:
        connection.close()
    assert value == "v"
    assert analytics_db.DEFAULT_ANALYTICS_DB.as_posix() == "db/analytics.db"
