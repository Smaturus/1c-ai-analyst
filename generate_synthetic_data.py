"""Генератор синтетического демо-датасета для проекта (без реальных данных 1С).

Создаёт ``sample_data.csv`` с колонками::

    date, product, counterparty, quantity, revenue, product_group

Все товары, контрагенты, группы и суммы вымышлены; данные генерируются
детерминированно (фиксированное семя), поэтому контрольные суммы стабильны.
Даты — 2023–2024, количество — 1–20, выручка = цена × количество; 10 % строк
отрицательные (возвраты).

Запуск::

    python generate_synthetic_data.py

Файл ``sample_data.csv`` безопасен для публикации и коммитится в репозиторий.
Реальные выгрузки 1С/Excel (``тест.xlsx``, ``1c_export/``) в репозиторий не
попадают — см. ``.gitignore``.
"""

from __future__ import annotations

import random
from datetime import date
from pathlib import Path

import pandas as pd

__all__ = ["SEED", "ROWS", "NEGATIVE_SHARE", "SAMPLE_CSV", "generate_sample_data"]

SEED = 20241024
ROWS = 600
"""Сколько строк генерировать."""

NEGATIVE_SHARE = 0.1
"""Доля строк с отрицательной выручкой (возвраты)."""

SAMPLE_CSV = Path(__file__).resolve().parent / "sample_data.csv"

PRODUCTS: tuple[tuple[str, str, float], ...] = (
    ("Смартфон Alpha", "Электроника", 45_000.0),
    ("Ноутбук Beta", "Электроника", 70_000.0),
    ("Наушники Gamma", "Аксессуары", 5_000.0),
    ("Планшет Delta", "Электроника", 30_000.0),
    ("Умные часы Epsilon", "Электроника", 15_000.0),
    ("Телевизор Zeta", "Электроника", 55_000.0),
    ("Чехол Eta", "Аксессуары", 1_200.0),
    ("Зарядное устройство Theta", "Аксессуары", 2_500.0),
    ("Кабель Iota", "Аксессуары", 700.0),
    ("Мышь Kappa", "Аксессуары", 1_800.0),
    ("Клавиатура Lambda", "Аксессуары", 3_500.0),
    ("Колонка Mu", "Аксессуары", 4_500.0),
)
"""Вымышленные товары: (название, группа, цена)."""

COUNTERPARTIES: tuple[str, ...] = (
    "ООО Ромашка",
    "ИП Иванов",
    "ООО Вектор",
    "ООО Лютик",
    "ИП Петров",
    "ООО Кристалл",
)
"""Вымышленные контрагенты."""

COLUMNS: tuple[str, ...] = (
    "date",
    "product",
    "counterparty",
    "quantity",
    "revenue",
    "product_group",
)
"""Схема ``sample_data.csv`` (порядок колонок фиксирован)."""

START = date(2023, 1, 1)
MONTHS = 24
"""Период: 2023-01-01 … 2024-12-31."""


def generate_sample_data(rows: int = ROWS, seed: int = SEED) -> pd.DataFrame:
    """Сгенерировать синтетическую витрину (по умолчанию 600 строк).

    Parameters
    ----------
    rows:
        Количество строк (по умолчанию :data:`ROWS`).
    seed:
        Семя генератора: при одном и том же семени результат детерминирован.

    Returns
    -------
    pandas.DataFrame
        Колонки :data:`COLUMNS`; ``date`` — ``datetime64[ns]``,
        ``quantity`` — целые числа, ``revenue`` — ``float64``.
    """
    if rows <= 0:
        raise ValueError(f"rows должен быть положительным, получено {rows}")
    rng = random.Random(seed)

    negative_count = max(1, round(rows * NEGATIVE_SHARE))
    negative_indices = set(rng.sample(range(rows), negative_count))

    records: list[dict[str, object]] = []
    for index in range(rows):
        product, group, price = PRODUCTS[index % len(PRODUCTS)]
        counterparty = COUNTERPARTIES[index % len(COUNTERPARTIES)]
        month_index = index % MONTHS
        document_date = date(
            START.year + month_index // 12,
            month_index % 12 + 1,
            rng.randint(1, 28),
        )
        if index in negative_indices:
            # Возврат: отрицательное количество и, соответственно, выручка.
            quantity = -rng.randint(1, 5)
        else:
            quantity = rng.randint(1, 20)
        records.append(
            {
                "date": document_date,
                "product": product,
                "counterparty": counterparty,
                "quantity": quantity,
                "revenue": round(price * quantity, 2),
                "product_group": group,
            }
        )

    frame = pd.DataFrame(records, columns=list(COLUMNS))
    frame["date"] = pd.to_datetime(frame["date"]).dt.normalize()
    frame["quantity"] = frame["quantity"].astype("int64")
    frame["revenue"] = frame["revenue"].astype("float64")
    frame["product"] = frame["product"].astype("string")
    frame["counterparty"] = frame["counterparty"].astype("string")
    frame["product_group"] = frame["product_group"].astype("string")
    return frame.reset_index(drop=True)


if __name__ == "__main__":
    data = generate_sample_data()
    data.to_csv(SAMPLE_CSV, index=False, encoding="utf-8")
    negatives = int((data["revenue"] < 0).sum())
    print(f"Сгенерирован {SAMPLE_CSV.name}: {len(data)} строк")
    print(f"  товаров: {data['product'].nunique()}, контрагентов: {data['counterparty'].nunique()}, "
          f"групп: {data['product_group'].nunique()}")
    print(f"  период: {data['date'].min():%d.%m.%Y} – {data['date'].max():%d.%m.%Y}")
    print(f"  выручка: {data['revenue'].sum():,.2f}, количество: {data['quantity'].sum():,}")
    print(f"  отрицательных строк (возвраты): {negatives} ({negatives / len(data) * 100:.0f} %)")
