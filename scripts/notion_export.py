#!/usr/bin/env python3
"""CLI-обёртка экспорта сессий в Notion.

Логика живёт в :mod:`onec_analyst.notion_export` (её же использует кнопка
«Выгрузить» в UI). Этот файл нужен, чтобы запускать экспорт из терминала и по
расписанию: ``python scripts/notion_export.py --limit 20``.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:  # запуск как скрипта, без установки пакета
    sys.path.insert(0, str(_SRC))

from onec_analyst.notion_export import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
