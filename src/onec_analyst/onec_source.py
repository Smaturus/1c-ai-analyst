"""Источник данных из 1С: выгрузка регистра «Выручка и себестоимость продаж».

Модуль дополняет :mod:`data_loader` (Excel-выгрузка) и решает две проблемы
исходного файла ``тест.xlsx``:

1. **Даты.** В Excel-выгрузке у 250 строк (24.4% выручки) пуста колонка
   «Заказ клиента», поэтому их нельзя отнести к периоду. В самом регистре у
   каждой записи есть ``Период`` (дата движения = дата документа), так что
   выгрузка из 1С даёт дату для 100% строк.
2. **Товарные группы.** В Excel нет справочника групп. В 1С товарная группа --
   это иерархия справочника «Номенклатура» (``Родитель``), стандартный способ
   группировки продаж (см. ИТС/Инфостарт: «Виды номенклатуры» -- учётная
   типизация, «Товарные категории» -- маркетинговый разрез, а группировка
   продаж делается по иерархии номенклатуры).

Режим работы -- двухшаговый, потому что 1С доступна только через COM
(``V83.COMConnector``) и требует пакета ``pywin32``:

1. **Выгрузка** (нужен Python с ``pywin32`` и лицензия 1С)::

       python onec_source.py --base "<путь к базе 1С>" --out-dir 1c_export \\
           --start 2022-01-01 --end 2024-12-31

   Создаются ``sales_1c.csv`` и ``nomenclature_1c.csv``.

2. **Загрузка** (обычный Python с pandas, без pywin32)::

       df = load_vitrina("1c_export/sales_1c.csv")

   Возвращается витрина с колонками ``date, product, counterparty, quantity,
   revenue, product_group`` -- та же схема, что у
   :func:`data_loader.load_and_clean_1c_report`, плюс реальная товарная группа.

Если ``pywin32`` установлен в рабочее окружение проекта (``pip install
pywin32``), шаг выгрузки можно вызывать напрямую из Python:
:func:`export_from_1c`.

Важно про выручку: по умолчанию берётся **сумма выручки без НДС**
(``revenue_mode="net"``). Это управленческий показатель: НДС -- обязательство
перед бюджетом, а не доход; выручка в бухучёте и ОФР показывается без НДС.
Для денежных потоков и сумм договоров используйте ``revenue_mode="gross"``.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any, Iterable, Literal

__all__ = [
    "DEFAULT_USR",
    "REGISTER_NAME",
    "CONFIG_EXPECTED",
    "connect",
    "describe_base",
    "fetch_nomenclature",
    "fetch_sales",
    "resolve_product_groups",
    "export_from_1c",
    "load_vitrina",
    "load_sample_vitrina",
    "load_nomenclature",
    "SALES_CSV",
    "NOMENCLATURE_CSV",
    "EXPORT_META_SUFFIX",
    "VITRINA_COLUMNS",
    "SAMPLE_COLUMNS",
    "meta_path_for",
    "read_export_meta",
    "write_export_meta",
    "parse_exported_at",
]

DEFAULT_USR: str = "Администратор"
"""Пользователь демо-базы 1С (в utdemo пароль пустой)."""

DEFAULT_PWD: str = ""
REGISTER_NAME: str = "РегистрНакопления.ВыручкаИСебестоимостьПродаж"
CONFIG_EXPECTED: str = "УправлениеТорговлей"
"""Ожидаемое имя конфигурации: «Управление торговлей, редакция 11»."""

SALES_CSV: str = "sales_1c.csv"
NOMENCLATURE_CSV: str = "nomenclature_1c.csv"
EXPORT_META_SUFFIX: str = ".meta.json"
"""Суффикс файла метаданных выгрузки: ``sales_1c.csv.meta.json``.

Дата выгрузки не должна подменяться датой последней продажи: метаданные лежат
рядом с CSV и читаются только для отображения, в расчётах не участвуют.
"""

RevenueMode = Literal["net", "gross"]

VITRINA_COLUMNS: tuple[str, ...] = (
    "date",
    "product",
    "counterparty",
    "quantity",
    "revenue",
    "product_group",
)
"""Колонки витрины, которую возвращает :func:`load_vitrina`."""

SAMPLE_COLUMNS: tuple[str, ...] = VITRINA_COLUMNS
"""Колонки синтетического ``sample_data.csv`` (см. ``generate_synthetic_data.py``)."""

UNKNOWN_PRODUCT: str = "Не указано"
UNGROUPED: str = "Без группы"

_CSV_FIELDS: tuple[str, ...] = (
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


# --------------------------------------------------------------------------- #
# Подключение к 1С (COM)
# --------------------------------------------------------------------------- #
def connect(base_path: str, *, usr: str = DEFAULT_USR, pwd: str = DEFAULT_PWD):
    """Подключиться к файловой базе 1С через ``V83.COMConnector``.

    Parameters
    ----------
    base_path:
        Путь к каталогу файловой базы, например ``<путь к базе 1С>``.
    usr, pwd:
        Пользователь и пароль 1С. В демо-базе ``utdemo`` -- ``Администратор``
        с пустым паролем.

    Returns
    -------
    COM-объект соединения 1С.

    Raises
    ------
    RuntimeError
        Если не установлен ``pywin32`` или не найдена лицензия 1С.
    """
    try:
        import win32com.client as win32_client
    except ImportError as exc:  # pragma: no cover - зависит от окружения
        raise RuntimeError(
            "Для подключения к 1С нужен pywin32: pip install pywin32. "
            "Альтернатива -- выгрузить CSV скриптом под другим Python и затем "
            "читать его функцией load_vitrina()."
        ) from exc

    try:  # COM в потоке обязательно инициализировать
        import pythoncom

        pythoncom.CoInitialize()
    except ImportError:  # pragma: no cover
        pass

    connection_string = (
        f'File="{Path(base_path)}";Usr="{usr}";Pwd="{pwd}";'
    )
    try:
        connector = win32_client.Dispatch("V83.COMConnector")
        return connector.Connect(connection_string)
    except Exception as exc:  # noqa: BLE001 - COM поднимает com_error
        raise RuntimeError(
            "Не удалось подключиться к 1С. Проверьте, что: 1) база не занята "
            "эксклюзивно; 2) у процесса есть права на запись в каталог базы "
            "(файловой 1С нужны lock-файлы); 3) действует лицензия 1С; "
            f"4) верны пользователь и пароль. Исходная ошибка: {exc}"
        ) from exc


def describe_base(conn) -> dict[str, str]:
    """Вернуть имя, синоним и версию конфигурации подключённой базы."""
    metadata = conn.Метаданные
    return {
        "name": str(metadata.Имя),
        "synonym": str(metadata.Синоним),
        "version": str(metadata.Версия),
    }


# --------------------------------------------------------------------------- #
# Выгрузка данных
# --------------------------------------------------------------------------- #
def fetch_sales(
    conn,
    *,
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
) -> list[dict[str, Any]]:
    """Прочитать движения регистра «Выручка и себестоимость продаж».

    Parameters
    ----------
    conn:
        Соединение 1С из :func:`connect`.
    start_date, end_date:
        Границы периода включительно; ``None`` -- без ограничения.

    Returns
    -------
    list of dict
        По одной записи регистра на элемент: дата, товар, контрагент,
        количество, выручка с НДС и без НДС, хозяйственная операция,
        регистратор, организация.
    """
    condition = _period_condition(start_date, end_date)
    query = conn.NewObject("Запрос")
    query.Текст = f"""
        ВЫБРАТЬ
            Период КАК Период,
            АналитикаУчетаНоменклатуры.Номенклатура КАК Номенклатура,
            АналитикаУчетаПоПартнерам.Партнер КАК Партнер,
            АналитикаУчетаПоПартнерам.Организация КАК Организация,
            Количество КАК Кол,
            СуммаВыручки КАК ВыручкаСНДС,
            СуммаВыручкиБезНДС КАК ВыручкаБезНДС,
            ПРЕДСТАВЛЕНИЕ(ХозяйственнаяОперация) КАК Операция,
            ПРЕДСТАВЛЕНИЕ(Регистратор) КАК Регистратор
        ИЗ {REGISTER_NAME}
        {condition}
        УПОРЯДОЧИТЬ ПО Период, Номенклатура
    """
    selection = query.Выполнить().Выбрать()
    records: list[dict[str, Any]] = []
    while selection.Следующий():
        records.append(
            {
                "date": _as_date(selection.Период),
                "product": _ref_name(selection.Номенклатура),
                "counterparty": _ref_name(selection.Партнер),
                "organization": _ref_name(selection.Организация),
                "quantity": _as_float(selection.Кол),
                "revenue_gross": _as_float(selection.ВыручкаСНДС),
                "revenue_net": _as_float(selection.ВыручкаБезНДС),
                "operation": str(selection.Операция or ""),
                "registrar": str(selection.Регистратор or ""),
            }
        )
    return records


def fetch_nomenclature(conn) -> list[dict[str, Any]]:
    """Прочитать справочник номенклатуры для построения товарных групп.

    Returns
    -------
    list of dict
        ``name``, ``parent``, ``is_group`` по всем элементам справочника.
    """
    query = conn.NewObject("Запрос")
    query.Текст = """
        ВЫБРАТЬ
            Наименование КАК Наименование,
            Родитель КАК Родитель,
            ЭтоГруппа КАК ЭтоГруппа
        ИЗ Справочник.Номенклатура
        УПОРЯДОЧИТЬ ПО Наименование
    """
    selection = query.Выполнить().Выбрать()
    items: list[dict[str, Any]] = []
    while selection.Следующий():
        items.append(
            {
                "name": str(selection.Наименование),
                "parent": _ref_name(selection.Родитель),
                "is_group": bool(selection.ЭтоГруппа),
            }
        )
    return items


def resolve_product_groups(
    nomenclature: Iterable[dict[str, Any]],
) -> dict[str, tuple[str, str]]:
    """Сопоставить каждому товару его товарную группу и полный путь.

    Товарная группа -- ближайшая группа-родитель в иерархии справочника
    «Номенклатура» (стандартный способ группировки продаж в УТ). Для товаров,
    лежащих в корне справочника, группа -- :data:`UNGROUPED`.

    Returns
    -------
    dict
        ``{товар: (группа, "Корень / ... / Группа")}``.
    """
    parents: dict[str, str | None] = {}
    groups: set[str] = set()
    for item in nomenclature:
        name = str(item["name"])
        parent = item.get("parent") or None
        parents[name] = str(parent) if parent else None
        if item.get("is_group"):
            groups.add(name)

    resolved: dict[str, tuple[str, str]] = {}
    for product in parents:
        if product in groups:
            continue
        parent = parents.get(product)
        if not parent:
            resolved[product] = (UNGROUPED, UNGROUPED)
            continue
        path: list[str] = []
        current: str | None = parent
        visited: set[str] = set()
        while current and current not in visited:
            visited.add(current)
            path.append(current)
            current = parents.get(current)
        resolved[product] = (path[0], " / ".join(reversed(path)))
    return resolved


# --------------------------------------------------------------------------- #
# Метаданные выгрузки: дата выгрузки, а не дата последней продажи
# --------------------------------------------------------------------------- #
def meta_path_for(sales_csv: str | Path) -> Path:
    """Путь к файлу метаданных рядом с CSV выгрузки."""
    path = Path(sales_csv)
    return path.with_name(path.name + EXPORT_META_SUFFIX)


def write_export_meta(
    sales_csv: str | Path,
    *,
    rows: int,
    revenue_mode: RevenueMode | None = None,
    source: str = "1c",
    exported_at: dt.datetime | None = None,
    extra: dict[str, Any] | None = None,
) -> Path:
    """Записать метаданные выгрузки рядом с CSV.

    ``exported_at`` — момент выгрузки, а не дата последней продажи. Если момент
    не передан, берётся текущее локальное время с часовым поясом.
    """
    moment = exported_at or dt.datetime.now().astimezone()
    payload: dict[str, Any] = {
        "exported_at": moment.isoformat(timespec="seconds"),
        "rows": int(rows),
        "source": source,
        "csv": Path(sales_csv).name,
    }
    if revenue_mode is not None:
        payload["revenue_mode"] = revenue_mode
    if extra:
        payload.update(extra)
    target = meta_path_for(sales_csv)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


def read_export_meta(sales_csv: str | Path) -> dict[str, Any] | None:
    """Прочитать метаданные выгрузки; ``None``, если файла нет или он повреждён."""
    target = meta_path_for(sales_csv)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _exported_at_attrs(sales_csv: Path, meta: dict[str, Any] | None) -> dict[str, Any]:
    """Дата данных для ``DataFrame.attrs`` в JSON-безопасном виде.

    ``DataFrame.attrs`` сериализуются Streamlit, поэтому ``datetime`` там
    недопустим: attrs молча теряются. Храним ISO-строку, а разбирает её
    потребитель через :func:`parse_exported_at`.
    """
    moment = parse_exported_at(_meta_exported_at(meta))
    if moment is not None:
        return {
            "exported_at": moment.isoformat(timespec="seconds"),
            "exported_at_source": "meta",
        }
    try:
        mtime = dt.datetime.fromtimestamp(sales_csv.stat().st_mtime)
    except OSError:
        return {"exported_at": None, "exported_at_source": "unknown"}
    return {
        "exported_at": mtime.isoformat(timespec="seconds"),
        "exported_at_source": "file_mtime",
    }


def parse_exported_at(value: str | None) -> dt.datetime | None:
    """Разобрать ISO-строку даты выгрузки; пустое или битое значение — ``None``."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone()
    return moment


def _meta_exported_at(meta: dict[str, Any] | None) -> str | None:
    if not meta:
        return None
    raw = meta.get("exported_at")
    return raw if isinstance(raw, str) else None


def export_from_1c(
    base_path: str,
    out_dir: str,
    *,
    usr: str = DEFAULT_USR,
    pwd: str = DEFAULT_PWD,
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
) -> dict[str, Any]:
    """Выгрузить движения регистра и справочник номенклатуры в CSV.

    Returns
    -------
    dict
        Статистика выгрузки: конфигурация, число записей, итоги, пути файлов.
    """
    conn = connect(base_path, usr=usr, pwd=pwd)
    config = describe_base(conn)
    records = fetch_sales(conn, start_date=start_date, end_date=end_date)
    nomenclature = fetch_nomenclature(conn)
    groups = resolve_product_groups(nomenclature)

    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    sales_path = directory / SALES_CSV
    nomenclature_path = directory / NOMENCLATURE_CSV

    with sales_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(_CSV_FIELDS))
        writer.writeheader()
        for record in records:
            group, group_path = groups.get(record["product"], (UNGROUPED, UNGROUPED))
            writer.writerow(
                {
                    "date": record["date"].isoformat(),
                    "product": record["product"],
                    "product_group": group,
                    "product_group_path": group_path,
                    "counterparty": record["counterparty"],
                    "quantity": record["quantity"],
                    "revenue_net": record["revenue_net"],
                    "revenue_gross": record["revenue_gross"],
                    "operation": record["operation"],
                    "registrar": record["registrar"],
                    "organization": record["organization"],
                }
            )

    with nomenclature_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["name", "parent", "is_group", "product_group", "product_group_path"])
        writer.writeheader()
        for item in nomenclature:
            group, group_path = groups.get(item["name"], (UNGROUPED, UNGROUPED))
            writer.writerow(
                {
                    "name": item["name"],
                    "parent": item.get("parent") or "",
                    "is_group": int(bool(item.get("is_group"))),
                    "product_group": group,
                    "product_group_path": group_path,
                }
            )

    stats: dict[str, Any] = {
        "config": config,
        "rows": len(records),
        "nomenclature": len(nomenclature),
        "groups": len({group for group, _ in groups.values()}),
        "revenue_net": round(sum(record["revenue_net"] for record in records), 2),
        "revenue_gross": round(sum(record["revenue_gross"] for record in records), 2),
        "quantity": round(sum(record["quantity"] for record in records), 3),
        "sales_csv": str(sales_path),
        "nomenclature_csv": str(nomenclature_path),
        "start_date": start_date.isoformat() if start_date else None,
        "end_date": end_date.isoformat() if end_date else None,
    }
    meta_file = write_export_meta(
        sales_path,
        rows=len(records),
        source="1c",
        extra={
            "config": config,
            "start_date": stats["start_date"],
            "end_date": stats["end_date"],
            "nomenclature": len(nomenclature),
        },
    )
    stats["meta_json"] = str(meta_file)
    return stats


# --------------------------------------------------------------------------- #
# Загрузка выгрузки в pandas (без pywin32)
# --------------------------------------------------------------------------- #
def load_vitrina(
    sales_csv: str,
    *,
    revenue_mode: RevenueMode = "net",
) -> "Any":
    """Прочитать CSV выгрузки 1С и вернуть витрину для :mod:`analytics`.

    Parameters
    ----------
    sales_csv:
        Путь к ``sales_1c.csv`` (результат :func:`export_from_1c`).
    revenue_mode:
        ``"net"`` (по умолчанию) -- выручка без НДС, ``"gross"`` -- с НДС.

    Returns
    -------
    pandas.DataFrame
        Колонки :data:`VITRINA_COLUMNS`: ``date`` (``datetime64``),
        ``product``/``counterparty``/``product_group`` (``string``),
        ``quantity``/``revenue`` (``float64``).
    """
    import pandas as pd

    if revenue_mode not in ("net", "gross"):
        raise ValueError(
            f"revenue_mode должен быть 'net' или 'gross', получено {revenue_mode!r}"
        )
    path = Path(sales_csv)
    if not path.is_file():
        raise FileNotFoundError(f"Файл выгрузки 1С не найден: {path}")

    frame = pd.read_csv(
        path,
        dtype={
            "product": "string",
            "counterparty": "string",
            "product_group": "string",
            "product_group_path": "string",
            "operation": "string",
            "registrar": "string",
            "organization": "string",
        },
        parse_dates=["date"],
    )
    revenue_column = "revenue_net" if revenue_mode == "net" else "revenue_gross"
    vitrina = pd.DataFrame(
        {
            "date": pd.to_datetime(frame["date"]).dt.normalize(),
            "product": frame["product"].fillna(UNKNOWN_PRODUCT),
            "counterparty": frame["counterparty"].fillna(UNKNOWN_PRODUCT),
            "quantity": pd.to_numeric(frame["quantity"], errors="coerce").fillna(0.0).astype("float64"),
            "revenue": pd.to_numeric(frame[revenue_column], errors="coerce").fillna(0.0).astype("float64"),
            "product_group": frame["product_group"].fillna(UNGROUPED),
        }
    )
    vitrina = vitrina.loc[:, list(VITRINA_COLUMNS)]
    vitrina = vitrina.sort_values(
        ["date", "product", "counterparty"],
        na_position="last",
        kind="stable",
    ).reset_index(drop=True)
    vitrina.attrs.update(
        {
            "source_file": str(path),
            "source": "1c",
            "revenue_mode": revenue_mode,
            "rows": len(vitrina),
            **_exported_at_attrs(path, read_export_meta(path)),
        }
    )
    return vitrina


def load_sample_vitrina(sample_csv: str = "sample_data.csv") -> "Any":
    """Прочитать синтетический ``sample_data.csv`` в витрину для аналитики.

    Файл генерируется :mod:`generate_synthetic_data` и содержит только
    вымышленные данные (без реальных выгрузок 1С). Схема совпадает с
    :data:`VITRINA_COLUMNS`, поэтому результат подходит для всех интентов
    :mod:`analytics` и для демонстрационного запуска Streamlit.
    """
    import pandas as pd

    path = Path(sample_csv)
    if not path.is_file():
        raise FileNotFoundError(f"Файл демо-данных не найден: {path}")
    frame = pd.read_csv(
        path,
        dtype={
            "product": "string",
            "counterparty": "string",
            "product_group": "string",
        },
        parse_dates=["date"],
    )
    missing = [name for name in SAMPLE_COLUMNS if name not in frame.columns]
    if missing:
        raise ValueError(
            f"В {path} не хватает колонок {missing}; ожидается схема {list(SAMPLE_COLUMNS)}"
        )
    vitrina = pd.DataFrame(
        {
            "date": pd.to_datetime(frame["date"]).dt.normalize(),
            "product": frame["product"].fillna(UNKNOWN_PRODUCT),
            "counterparty": frame["counterparty"].fillna(UNKNOWN_PRODUCT),
            "quantity": pd.to_numeric(frame["quantity"], errors="coerce")
            .fillna(0.0)
            .astype("float64"),
            "revenue": pd.to_numeric(frame["revenue"], errors="coerce")
            .fillna(0.0)
            .astype("float64"),
            "product_group": frame["product_group"].fillna(UNGROUPED),
        }
    )
    vitrina = vitrina.loc[:, list(VITRINA_COLUMNS)]
    vitrina = vitrina.sort_values(
        ["date", "product", "counterparty"],
        na_position="last",
        kind="stable",
    ).reset_index(drop=True)
    vitrina.attrs.update(
        {
            "source_file": str(path),
            "source": "synthetic",
            "rows": len(vitrina),
            **_exported_at_attrs(path, read_export_meta(path)),
        }
    )
    return vitrina


def load_nomenclature(nomenclature_csv: str) -> "Any":
    """Прочитать ``nomenclature_1c.csv`` (справочник с группами) в DataFrame."""
    import pandas as pd

    path = Path(nomenclature_csv)
    if not path.is_file():
        raise FileNotFoundError(f"Файл справочника номенклатуры не найден: {path}")
    return pd.read_csv(path, dtype={"name": "string", "parent": "string"}, keep_default_na=False)


# --------------------------------------------------------------------------- #
# Приватные помощники
# --------------------------------------------------------------------------- #
def _period_condition(start_date: dt.date | None, end_date: dt.date | None) -> str:
    """Собрать условие ``ГДЕ Период МЕЖДУ ...`` (или пустую строку)."""
    if start_date is not None and end_date is not None and start_date > end_date:
        raise ValueError(f"start_date ({start_date}) позже end_date ({end_date})")
    if start_date is None and end_date is None:
        return ""
    parts: list[str] = []
    if start_date is not None:
        parts.append(
            f"Период >= ДАТАВРЕМЯ({start_date.year}, {start_date.month}, {start_date.day})"
        )
    if end_date is not None:
        parts.append(
            "Период <= ДАТАВРЕМЯ("
            f"{end_date.year}, {end_date.month}, {end_date.day}, 23, 59, 59)"
        )
    return "ГДЕ " + " И ".join(parts)


def _as_date(value: Any) -> dt.date:
    """Привести дату из COM (``pywintypes.datetime``) к ``datetime.date``."""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = str(value)
    return dt.date.fromisoformat(text[:10])


def _as_float(value: Any) -> float:
    """Привести число из COM к ``float`` (включая русскую десятичную запятую)."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("\u00a0", "").replace(" ", "")
    if not text:
        return 0.0
    if "," in text and "." in text:
        text = text.replace(",", "")
    else:
        text = text.replace(",", ".")
    return float(text)


def _ref_name(value: Any) -> str:
    """Представление ссылки 1С: ``Наименование`` или пустая строка."""
    if value is None:
        return ""
    try:
        return str(getattr(value, "Наименование"))
    except Exception:  # noqa: BLE001 - не у всех ссылок есть Наименование
        try:
            return str(value)
        except Exception:  # pragma: no cover
            return ""


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _parse_date(text: str | None) -> dt.date | None:
    if text is None:
        return None
    return dt.date.fromisoformat(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Выгрузка регистра «Выручка и себестоимость продаж» из 1С в CSV."
    )
    parser.add_argument("--base", required=True, help="каталог файловой базы 1С, например <путь к базе 1С>")
    parser.add_argument("--out-dir", default="1c_export", help="каталог для CSV (по умолчанию 1c_export)")
    parser.add_argument("--usr", default=DEFAULT_USR, help="пользователь 1С")
    parser.add_argument("--pwd", default=DEFAULT_PWD, help="пароль 1С")
    parser.add_argument("--start", default=None, help="начало периода, ГГГГ-ММ-ДД")
    parser.add_argument("--end", default=None, help="конец периода включительно, ГГГГ-ММ-ДД")
    args = parser.parse_args(argv)

    stats = export_from_1c(
        args.base,
        args.out_dir,
        usr=args.usr,
        pwd=args.pwd,
        start_date=_parse_date(args.start),
        end_date=_parse_date(args.end),
    )
    print(f"Конфигурация: {stats['config']['synonym']} {stats['config']['version']}")
    print(f"Записей регистра: {stats['rows']}")
    print(f"Номенклатуры: {stats['nomenclature']}, товарных групп: {stats['groups']}")
    print(f"Выручка без НДС: {stats['revenue_net']:,.2f}")
    print(f"Выручка с НДС:   {stats['revenue_gross']:,.2f}")
    print(f"Количество:      {stats['quantity']:,.3f}")
    print(f"CSV: {stats['sales_csv']}")
    print(f"CSV: {stats['nomenclature_csv']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
