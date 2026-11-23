#!/usr/bin/env python3
"""Загрузка выгрузки 1С в отдельную SQLite-БД (архив/SQL-слой).

Запуск вручную; приложение этот скрипт не вызывает и продолжает читать CSV.

Примеры::

    python scripts/load_1c_to_sqlite.py                 # перезагрузить db/analytics.db
    python scripts/load_1c_to_sqlite.py --stats         # только посмотреть, что в БД
    python scripts/load_1c_to_sqlite.py --sales 1c_export/sales_1c.csv
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:  # запуск как скрипта, без установки пакета
    sys.path.insert(0, str(_SRC))

from onec_analyst.analytics_db import (  # noqa: E402
    DEFAULT_NOMENCLATURE_CSV,
    DEFAULT_SALES_CSV,
    refresh,
    resolve_db_path,
    stats,
)

logger = logging.getLogger("load_1c_to_sqlite")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Загрузка выгрузки 1С в SQLite")
    parser.add_argument(
        "--nomenclature",
        default=str(_REPO_ROOT / DEFAULT_NOMENCLATURE_CSV),
        help="CSV номенклатуры",
    )
    parser.add_argument(
        "--sales",
        default=str(_REPO_ROOT / DEFAULT_SALES_CSV),
        help="CSV продаж",
    )
    parser.add_argument("--db", default=None, help="путь к analytics.db (по умолчанию db/analytics.db)")
    parser.add_argument("--stats", action="store_true", help="только показать содержимое БД")
    parser.add_argument("--verbose", action="store_true", help="подробный лог")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.stats:
        print(json.dumps(stats(args.db), ensure_ascii=False, indent=2))
        return 0

    try:
        result = refresh(
            nomenclature_csv=Path(args.nomenclature),
            sales_csv=Path(args.sales),
            db_path=args.db,
        )
    except (FileNotFoundError, ValueError) as exc:
        logger.error("Загрузка не выполнена: %s", exc)
        return 2

    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    logger.info("Готово: %s", resolve_db_path() if args.db is None else args.db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
