"""Загрузка и очистка Excel-выгрузки из 1С.

Модуль решает ровно одну задачу ETL: превратить «Универсальный отчёт» по
регистру накопления «Выручка и себестоимость продаж» в аккуратный
типизированный ``pandas.DataFrame``, пригодный для расчётов в
:mod:`analytics` и для отрисовки в Streamlit.

.. note::
   Если есть доступ к самой базе 1С, предпочтительнее :mod:`onec_source`:
   выгрузка напрямую из регистра даёт дату у 100% строк (в Excel-выгрузке
   ``Период`` отсутствует) и настоящие товарные группы из иерархии справочника
   «Номенклатура». Этот модуль остаётся для работы с уже готовыми Excel-файлами.

Особенности исходного файла
---------------------------
1. Первые 7 строк -- шапка отчёта (параметры, период, отборы), поэтому чтение
   идёт с ``skiprows=7``; строка с заголовками колонок (8-я по счёту) при этом
   становится заголовком таблицы.
2. Последняя строка -- итоговая строка ``Итого``; она удаляется, иначе агрегаты
   удвоятся.
3. Колонка ``A`` содержит склеенное значение ``"<товар>; <склад>"`` -- нужен
   только товар (до первого символа ``;``).
4. Колонка ``E`` содержит документ с датой, например
   ``"Реализация товаров и услуг ТД00-000029 от 21.09.2023 19:11:29"`` --
   дата извлекается регулярным выражением ``\\d{2}\\.\\d{2}\\.\\d{4}``.
   В части строк (розница, отчёты комиссионера, перемещения) эта колонка
   пуста, тогда дата остаётся ``NaT``.
5. Колонка ``V`` содержит склеенного партнёра
   ``"<контрагент>; <организация>; <договор>"`` -- нужен только контрагент.
6. ``W`` -- количество, ``X`` -- выручка с НДС, ``Y`` -- выручка без НДС.
7. Возвраты и сторно приходят с отрицательными суммами и не отбрасываются.

Результат
---------
DataFrame с колонками :data:`OUTPUT_COLUMNS`::

    date | product | counterparty | quantity | revenue

* ``date`` -- ``datetime64`` (только дата, без времени), ``NaT`` если в выгрузке
  нет документа;
* ``product``, ``counterparty`` -- ``string``, пустые значения заменены на
  :data:`MISSING_TEXT`;
* ``quantity``, ``revenue`` -- ``float64``, отрицательные значения сохраняются,
  отсутствующие числа заменяются на ``0.0`` (строка без суммы не влияет на
  агрегаты, но и не искажает их).

Модуль не выполняет агрегаций и не фильтрует данные по периодам -- это зона
ответственности :mod:`analytics`.
"""

from __future__ import annotations

import re
import sys
import warnings
import zipfile
from datetime import date
from pathlib import Path
from typing import Final, Literal, TypeAlias

import pandas as pd

__all__ = [
    "DATE",
    "PRODUCT",
    "COUNTERPARTY",
    "QUANTITY",
    "REVENUE",
    "OUTPUT_COLUMNS",
    "RevenueMode",
    "HEADER_ROWS",
    "MISSING_TEXT",
    "TOTAL_ROW_MARKER",
    "COL_PRODUCT_RAW",
    "COL_DOCUMENT_RAW",
    "COL_COUNTERPARTY_RAW",
    "COL_QUANTITY",
    "COL_REVENUE_GROSS",
    "COL_REVENUE_NET",
    "load_and_clean_1c_report",
]

# --------------------------------------------------------------------------- #
# Константы: позиции колонок в исходной выгрузке.
# Позиции фиксированы форматом отчёта 1С; имена заголовков используются только
# для валидации структуры файла (их может переименовать пользователь 1С).
# --------------------------------------------------------------------------- #
HEADER_ROWS: Final[int] = 7
"""Сколько строк шапки отчёта пропускать перед строкой заголовков."""

COL_PRODUCT_RAW: Final[int] = 0
"""``A``: склеенное значение ``"<товар>; <склад>"``."""

COL_DOCUMENT_RAW: Final[int] = 4
"""``E``: документ с датой ``"... от 21.09.2023 19:11:29"`` (может быть пустым)."""

COL_COUNTERPARTY_RAW: Final[int] = 21
"""``V``: склеенный партнёр ``"<контрагент>; <организация>; <договор>"``."""

COL_QUANTITY: Final[int] = 22
"""``W``: количество."""

COL_REVENUE_GROSS: Final[int] = 23
"""``X``: сумма выручки с НДС."""

COL_REVENUE_NET: Final[int] = 24
"""``Y``: сумма выручки без НДС."""

RevenueMode: TypeAlias = Literal["net", "gross"]
"""Какую колонку выручки брать: ``net`` -- без НДС (``Y``), ``gross`` -- с НДС (``X``)."""

_REVENUE_MODE_TO_COLUMN: Final[dict[str, int]] = {
    "net": COL_REVENUE_NET,
    "gross": COL_REVENUE_GROSS,
}

# --------------------------------------------------------------------------- #
# Константы: имена выходных колонок и служебные значения.
# --------------------------------------------------------------------------- #
DATE: Final[str] = "date"
PRODUCT: Final[str] = "product"
COUNTERPARTY: Final[str] = "counterparty"
QUANTITY: Final[str] = "quantity"
REVENUE: Final[str] = "revenue"

OUTPUT_COLUMNS: Final[tuple[str, ...]] = (DATE, PRODUCT, COUNTERPARTY, QUANTITY, REVENUE)

TOTAL_ROW_MARKER: Final[str] = "итого"
MISSING_TEXT: Final[str] = "Не указано"
LIST_SEPARATOR: Final[str] = ";"
DATE_REGEX: Final[re.Pattern[str]] = re.compile(r"(\d{2}\.\d{2}\.\d{4})")
"""Дата ``ДД.ММ.ГГГГ`` внутри документа; группа нужна для ``Series.str.extract``."""
DATE_FORMAT: Final[str] = "%d.%m.%Y"

# --------------------------------------------------------------------------- #
# Валидация структуры файла.
# --------------------------------------------------------------------------- #
_COLUMN_SIGNATURES: Final[dict[int, tuple[str, ...]]] = {
    COL_PRODUCT_RAW: ("номенклатур",),
    COL_DOCUMENT_RAW: ("документ", "заказ клиента"),
    COL_COUNTERPARTY_RAW: ("партнер", "контрагент"),
    COL_QUANTITY: ("количество",),
    COL_REVENUE_GROSS: ("выручк",),
    COL_REVENUE_NET: ("выручк",),
}
"""Ожидаемые фрагменты заголовков для контрольных колонок (в нижнем регистре)."""

_MIN_MATCHED_COLUMNS: Final[int] = 4
"""Сколько контрольных колонок должно совпасть, чтобы считать файл корректным."""


def load_and_clean_1c_report(
    file_path: str,
    *,
    sheet_name: str | int = 0,
    revenue_mode: RevenueMode = "net",
    header_rows: int = HEADER_ROWS,
) -> pd.DataFrame:
    """Прочитать Excel-выгрузку 1С и вернуть очищенный DataFrame.

    Parameters
    ----------
    file_path:
        Путь к файлу ``.xlsx`` (абсолютный или относительный). Тип ``str``, а не
        ``Path``, -- чтобы сигнатура совпадала с контрактом из ТЗ; ``Path`` тоже
        принимается на практике.
    sheet_name:
        Имя или индекс листа; по умолчанию первый лист.
    revenue_mode:
        ``"net"`` (по умолчанию) -- выручка без НДС (колонка ``Y``),
        ``"gross"`` -- выручка с НДС (колонка ``X``). В выгрузке это две разные
        колонки, поэтому режим вынесен в явный параметр.
    header_rows:
        Количество строк шапки (по умолчанию :data:`HEADER_ROWS` = 7).

    Returns
    -------
    pandas.DataFrame
        Колонки :data:`OUTPUT_COLUMNS`, отсортированные по ``date``, ``product``
        и ``counterparty``; строки без даты уходят в конец. Индекс --
        последовательный целочисленный.

    Raises
    ------
    FileNotFoundError
        Файл не найден.
    ValueError
        Файл не похож на выгрузку нужного отчёта (не сходится структура
        колонок) либо передан неизвестный ``revenue_mode``.

    Warns
    -----
    UserWarning
        В данных есть строки без даты, и часть выручки не попадёт в аналитику
        по периодам; также предупреждение о несовпадении заголовков колонок.

    Examples
    --------
    >>> df = load_and_clean_1c_report("тест.xlsx")  # doctest: +SKIP
    >>> list(df.columns)                            # doctest: +SKIP
    ['date', 'product', 'counterparty', 'quantity', 'revenue']
    """
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"Файл выгрузки не найден: {path}")

    raw = _read_report(path, sheet_name=sheet_name, header_rows=header_rows)
    _validate_layout(raw.columns, header_rows=header_rows)

    revenue_column = _resolve_revenue_column(revenue_mode, raw.columns)
    data, dropped_total, dropped_blank = _drop_service_rows(raw)

    result = _build_frame(data, revenue_column)
    _warn_about_quality(result, dropped_total=dropped_total, dropped_blank=dropped_blank)

    result.attrs.update(
        {
            "source_file": str(path),
            "revenue_mode": revenue_mode,
            "revenue_column_index": revenue_column,
            "header_rows": header_rows,
        }
    )
    return result


# --------------------------------------------------------------------------- #
# Приватные шаги ETL.
# --------------------------------------------------------------------------- #
def _read_report(path: Path, *, sheet_name: str | int, header_rows: int) -> pd.DataFrame:
    """Прочитать лист отчёта, пропустив шапку.

    Параметры ``header_rows`` и ``sheet_name`` передаются в ``read_excel``;
    ``engine="openpyxl"`` фиксируется явно, чтобы не зависеть от окружения.
    """
    if header_rows < 0:
        raise ValueError(f"header_rows не может быть отрицательным: {header_rows}")
    try:
        return pd.read_excel(
            path,
            sheet_name=sheet_name,
            skiprows=header_rows,
            engine="openpyxl",
        )
    except ImportError as exc:  # pragma: no cover - защита от неполного окружения
        raise RuntimeError(
            "Для чтения .xlsx нужен пакет openpyxl. Установите его: pip install openpyxl"
        ) from exc
    except zipfile.BadZipFile as exc:
        raise ValueError(
            f"Не удалось прочитать Excel-файл {path}: он повреждён или это не .xlsx"
        ) from exc


def _resolve_revenue_column(revenue_mode: str, columns: pd.Index) -> int:
    """Вернуть индекс колонки выручки по режиму ``net``/``gross``.

    Колонка выбирается строго по позиции: ``X`` (23) -- с НДС, ``Y`` (24) -- без
    НДС. Заголовок используется только как защита от ошибки: если колонка
    подписана противоположным образом (например, колонки X/Y перепутаны),
    поднимается ``ValueError`` -- молча подставлять «похожую» колонку в
    финансовых расчётах недопустимо.

    Raises
    ------
    ValueError
        Неизвестный режим или противоречие между позицией и заголовком колонки.
    """
    try:
        expected = _REVENUE_MODE_TO_COLUMN[revenue_mode]
    except KeyError:
        allowed = ", ".join(sorted(_REVENUE_MODE_TO_COLUMN))
        raise ValueError(
            f"revenue_mode={revenue_mode!r} не поддерживается, допустимо: {allowed}"
        ) from None

    header = "" if pd.isna(columns[expected]) else str(columns[expected]).casefold()
    marker = "без ндс" if revenue_mode == "net" else "с ндс"
    opposite = "с ндс" if revenue_mode == "net" else "без ндс"
    if "выручк" in header and opposite in header and marker not in header:
        raise ValueError(
            f"Колонка с индексом {expected} подписана {str(columns[expected])!r}, "
            f"что не соответствует revenue_mode={revenue_mode!r}. "
            "Похоже, колонки выручки сдвинуты или перепутаны: проверьте структуру выгрузки."
        )
    return expected


def _validate_layout(columns: pd.Index, *, header_rows: int) -> None:
    """Проверить, что файл похож на нужную выгрузку 1С.

    Имена заголовков может переименовать пользователь 1С, поэтому чтение данных
    идёт по позициям колонок, а заголовки используются только как признак
    структуры: если совпало меньше :data:`_MIN_MATCHED_COLUMNS` контрольных
    колонок, файл почти наверняка не тот (или сбилось число строк шапки).

    Raises
    ------
    ValueError
        Колонок меньше, чем требует схема, либо структура не распознана.

    Warns
    -----
    UserWarning
        Часть заголовков не совпала: данные прочитаны, но состав колонок стоит
        проверить.
    """
    if len(columns) <= COL_REVENUE_NET:
        raise ValueError(
            f"В файле {len(columns)} колонок, ожидалось минимум {COL_REVENUE_NET + 1}. "
            "Похоже, это не выгрузка регистра «Выручка и себестоимость продаж»."
        )

    matched: list[int] = []
    unmatched: list[int] = []
    for index_, signatures in _COLUMN_SIGNATURES.items():
        header = columns[index_]
        name = "" if pd.isna(header) else str(header).casefold()
        if any(signature in name for signature in signatures):
            matched.append(index_)
        else:
            unmatched.append(index_)

    if len(matched) < _MIN_MATCHED_COLUMNS:
        actual = [str(columns[index]) for index in range(min(len(columns), COL_REVENUE_NET + 1))]
        raise ValueError(
            "Структура файла не соответствует ожидаемой выгрузке 1С: "
            f"не найдены заголовки в колонках {sorted(unmatched)} "
            f"при header_rows={header_rows}. Фактические заголовки: {actual}. "
            "Проверьте, что это «Универсальный отчёт» по регистру "
            "«Выручка и себестоимость продаж», и при необходимости измените header_rows."
        )

    if unmatched:
        warnings.warn(
            f"Заголовки колонок {sorted(unmatched)} не совпали с ожидаемыми. "
            "Данные прочитаны по позициям колонок -- проверьте корректность выгрузки.",
            UserWarning,
            stacklevel=3,
        )


def _normalise_text(series: pd.Series) -> pd.Series:
    """Привести колонку к строковому типу и убрать «мусорные» пробелы.

    Заменяет неразрывные пробелы (``\\u00a0``), схлопывает повторяющиеся
    пробелы и обрезает пробелы по краям. ``NaN`` остаётся ``pd.NA``.
    """
    text = series.astype("string")
    text = text.str.replace("\u00a0", " ", regex=False)
    text = text.str.replace(r"\s+", " ", regex=True)
    return text.str.strip()


def _first_list_item(series: pd.Series) -> pd.Series:
    """Взять первый элемент склеенного через ``";"`` значения.

    ``"Ассоль; Торговый дом; Договор"`` -> ``"Ассоль"``.
    Пустые значения заменяются на :data:`MISSING_TEXT`.
    """
    first = _normalise_text(series).str.split(LIST_SEPARATOR, n=1).str[0]
    first = first.fillna("").str.strip()
    first = first.mask(first.eq(""), MISSING_TEXT)
    return first.astype("string")


def _extract_dates(series: pd.Series) -> pd.Series:
    """Извлечь дату ``ДД.ММ.ГГГГ`` из документа и нормализовать до дня.

    Основной путь -- регулярное выражение по тексту документа. Если в ячейке
    уже лежит дата (``datetime``/``date``), а не текст, она используется как есть:
    Excel-выгрузки иногда отдают дату типом, а не строкой.
    """
    text = _normalise_text(series)
    found = text.str.extract(DATE_REGEX.pattern, expand=False)
    parsed = pd.to_datetime(found, format=DATE_FORMAT, errors="coerce")

    is_date_cell = series.map(lambda value: isinstance(value, date))
    fallback = pd.to_datetime(series.where(is_date_cell), errors="coerce")
    return parsed.fillna(fallback).dt.normalize()


def _to_float(series: pd.Series) -> pd.Series:
    """Привести колонку к ``float64``, корректно обрабатывая текстовые числа.

    Поддерживаются как настоящие числа Excel, так и строки с пробелами-
    разделителями тысяч и десятичной запятой (``"1 234,56"``). Нераспознанные
    значения становятся ``NaN``.
    """
    if pd.api.types.is_numeric_dtype(series):
        return pd.to_numeric(series, errors="coerce").astype("float64")
    cleaned = (
        _normalise_text(series)
        .str.replace(" ", "", regex=False)
        .str.replace(",", ".", regex=False)
    )
    return pd.to_numeric(cleaned, errors="coerce").astype("float64")


def _drop_service_rows(raw: pd.DataFrame) -> tuple[pd.DataFrame, int, int]:
    """Удалить строку ``Итого`` и полностью пустые строки.

    Returns
    -------
    tuple[pandas.DataFrame, int, int]
        Очищенные данные, количество удалённых итоговых строк и количество
        удалённых пустых строк.
    """
    markers = _normalise_text(raw.iloc[:, COL_PRODUCT_RAW]).fillna("")
    normalised = markers.str.casefold().str.rstrip(": ")
    is_total = normalised.eq(TOTAL_ROW_MARKER) | (
        normalised.str.startswith(TOTAL_ROW_MARKER)
        & ~markers.str.contains(LIST_SEPARATOR, regex=False)
    )

    key_columns = [
        COL_PRODUCT_RAW,
        COL_DOCUMENT_RAW,
        COL_COUNTERPARTY_RAW,
        COL_QUANTITY,
        COL_REVENUE_GROSS,
        COL_REVENUE_NET,
    ]
    is_blank = raw.iloc[:, key_columns].isna().all(axis=1)

    keep = ~(is_total | is_blank)
    cleaned = raw.loc[keep].reset_index(drop=True)
    return cleaned, int(is_total.sum()), int((is_blank & ~is_total).sum())


def _build_frame(data: pd.DataFrame, revenue_column: int) -> pd.DataFrame:
    """Собрать итоговый DataFrame с контрактными именами колонок."""
    frame = pd.DataFrame(
        {
            DATE: _extract_dates(data.iloc[:, COL_DOCUMENT_RAW]),
            PRODUCT: _first_list_item(data.iloc[:, COL_PRODUCT_RAW]),
            COUNTERPARTY: _first_list_item(data.iloc[:, COL_COUNTERPARTY_RAW]),
            QUANTITY: _to_float(data.iloc[:, COL_QUANTITY]),
            REVENUE: _to_float(data.iloc[:, revenue_column]),
        }
    )
    if frame.empty:
        return frame.loc[:, list(OUTPUT_COLUMNS)].reset_index(drop=True)

    frame[QUANTITY] = frame[QUANTITY].fillna(0.0)
    frame[REVENUE] = frame[REVENUE].fillna(0.0)
    frame = frame.loc[:, list(OUTPUT_COLUMNS)]
    frame = frame.sort_values(
        [DATE, PRODUCT, COUNTERPARTY],
        na_position="last",
        kind="stable",
    ).reset_index(drop=True)
    return frame


def _warn_about_quality(
    frame: pd.DataFrame,
    *,
    dropped_total: int,
    dropped_blank: int,
) -> None:
    """Предупредить о найденных проблемах качества данных.

    Главная проблема выгрузки: в части строк пуста колонка ``E`` (``Заказ
    клиента``), поэтому у них нет даты. Такие строки сохраняются, но не попадают
    в аналитику по периодам -- об этом нужно знать пользователю.
    """
    if frame.empty:
        warnings.warn(
            "После очистки не осталось ни одной строки данных: проверьте файл выгрузки.",
            UserWarning,
            stacklevel=3,
        )
        return

    parts: list[str] = [
        f"Выгрузка 1С загружена: {len(frame)} строк, "
        f"{frame[PRODUCT].nunique(dropna=True)} товаров, "
        f"{frame[COUNTERPARTY].nunique(dropna=True)} контрагентов."
    ]
    if dropped_total:
        parts.append(f"Удалено служебных строк «Итого»: {dropped_total}.")
    if dropped_blank:
        parts.append(f"Удалено пустых строк: {dropped_blank}.")

    undated = frame[DATE].isna()
    undated_count = int(undated.sum())
    if undated_count:
        total_revenue = float(frame[REVENUE].sum())
        undated_revenue = float(frame.loc[undated, REVENUE].sum())
        share = (undated_revenue / total_revenue * 100.0) if total_revenue else 0.0
        parts.append(
            f"Без даты документа: {undated_count} строк "
            f"({undated_count / len(frame) * 100.0:.1f}%), "
            f"в них выручки {undated_revenue:.2f} ({share:.1f}% от суммы); "
            "эти строки не попадут в аналитику по периодам."
        )

    negative_count = int((frame[REVENUE] < 0).sum())
    if negative_count:
        parts.append(
            f"Строк с отрицательной выручкой (возвраты, сторно): {negative_count} — "
            "они учтены в расчётах со знаком «минус»."
        )

    warnings.warn(" ".join(parts), UserWarning, stacklevel=3)


if __name__ == "__main__":  # pragma: no cover - ручная проверка загрузчика
    if len(sys.argv) < 2:
        raise SystemExit("Использование: python data_loader.py <путь к .xlsx>")
    loaded = load_and_clean_1c_report(sys.argv[1])
    print(loaded.head(10).to_string())
    print()
    print(loaded.dtypes.to_string())
    print()
    print(f"Всего строк: {len(loaded)}")
