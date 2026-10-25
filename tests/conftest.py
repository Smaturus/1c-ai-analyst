"""Общие фикстуры pytest: синтетическая витрина без реальных файлов 1С.

Тесты не зависят от `тест.xlsx`, `1c_export/sales_1c.csv` и от win32com:
все расчёты проверяются на маленьком DataFrame с предсказуемыми суммами.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from analytics import (
    COUNTERPARTY,
    DATE,
    PRODUCT,
    PRODUCT_GROUP,
    QUANTITY,
    REVENUE,
)


def make_frame(rows: list[tuple[Any, ...]]) -> pd.DataFrame:
    """Собрать DataFrame витрины из строк ``(date, product, group, counterparty, qty, revenue)``."""
    frame = pd.DataFrame(
        rows,
        columns=[DATE, PRODUCT, PRODUCT_GROUP, COUNTERPARTY, QUANTITY, REVENUE],
    )
    frame[DATE] = pd.to_datetime(frame[DATE], errors="coerce")
    return frame


@pytest.fixture()
def sales_frame() -> pd.DataFrame:
    """Синтетическая витрина 2023–2024 с проверяемыми вручную суммами.

    Ожидаемые итоги:

    * 2023 — выручка 330, количество 33;
    * 2024 — выручка 180, количество 37;
    * контрагенты: ``A`` 100 → 120 (рост), ``B`` 200 → 40 (падение −160),
      ``C`` 30 → 20 (падение −10);
    * товарные группы: ``G1`` 100 → 120, ``G2`` 200 → 40, ``G3`` 30 → 20.
    """
    return make_frame(
        [
            ("2023-01-15", "P1", "G1", "A", 10, 100.0),
            ("2023-02-20", "P2", "G2", "B", 20, 200.0),
            ("2023-03-05", "P3", "G3", "C", 3, 30.0),
            ("2024-02-10", "P1", "G1", "A", 5, 120.0),
            ("2024-02-10", "P2", "G2", "B", 30, 40.0),
            ("2024-03-05", "P3", "G3", "C", 2, 20.0),
        ]
    )


@pytest.fixture()
def undated_frame(sales_frame: pd.DataFrame) -> pd.DataFrame:
    """Та же витрина плюс строка без даты — для проверки качества данных."""
    extra = make_frame([(None, "P4", "G4", "D", 5, 50.0)])
    return pd.concat([sales_frame, extra], ignore_index=True)


# --------------------------------------------------------------------------- #
# Учёт лимитов LLM: изолированная SQLite-база для каждого теста
# --------------------------------------------------------------------------- #
@pytest.fixture()
def rate_limit_db_path() -> Iterator[Path]:
    """Путь к временной SQLite-базе лимитов внутри рабочей папки.

    Системный ``%TEMP%`` в песочнице недоступен для SQLite (файл не создаётся),
    поэтому база кладётся в ``.pytest_tmp/`` проекта и удаляется после теста.
    """
    directory = Path(__file__).resolve().parents[1] / ".pytest_tmp"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rate_limit_{uuid.uuid4().hex}.db"
    yield path
    for suffix in ("", "-wal", "-shm"):
        try:
            path.with_name(path.name + suffix).unlink()
        except FileNotFoundError:
            pass


@pytest.fixture(autouse=True)
def _isolated_rate_limit_db(
    monkeypatch: pytest.MonkeyPatch, rate_limit_db_path: Path
) -> None:
    """Все тесты (включая AppTest) используют временную базу лимитов."""
    monkeypatch.setenv("RATE_LIMIT_DB", str(rate_limit_db_path))
