#!/usr/bin/env python3
"""Загрузка тестовых артефактов в `db/analytics.db` (отдельные таблицы).

Запуск вручную; приложение скрипт не вызывает.

Примеры::

    python scripts/load_testing_to_sqlite.py \
        --program "<путь>/Программа тестирования.xlsx" \
        --results "<путь>/Результаты тестирования.xlsx"

    python scripts/load_testing_to_sqlite.py --cassettes-dir tests/golden/cassettes
    python scripts/load_testing_to_sqlite.py --stats
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

from onec_analyst.testing_db import (  # noqa: E402
    DEFAULT_CASSETTES_DIR,
    import_testing,
    stats,
)

logger = logging.getLogger("load_testing_to_sqlite")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Загрузка тестовых артефактов в SQLite")
    parser.add_argument("--program", default=None, help="книга «Программа тестирования»")
    parser.add_argument("--results", default=None, help="книга «Результаты тестирования»")
    parser.add_argument(
        "--cassettes-dir",
        default=None,
        help=f"каталог golden-кассет (например {DEFAULT_CASSETTES_DIR.as_posix()})",
    )
    parser.add_argument("--db", default=None, help="путь к analytics.db")
    parser.add_argument("--stats", action="store_true", help="показать, что уже загружено")
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
        result = import_testing(
            program_xlsx=Path(args.program) if args.program else None,
            results_xlsx=Path(args.results) if args.results else None,
            cassettes_dir=Path(args.cassettes_dir) if args.cassettes_dir else None,
            db_path=args.db,
        )
    except (FileNotFoundError, ValueError) as exc:
        logger.error("Импорт не выполнен: %s", exc)
        return 2

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
