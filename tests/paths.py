"""Общие пути и строковые цели monkeypatch для AppTest.

Корень репозитория ищется по маркерам (``pyproject.toml`` / ``.git``), без
жёсткого ``parents[N]``. Константа патча — единая точка правды: не копировать
устаревшие имена вроде ``"clarification.choose_clarified_text"``.
"""

from __future__ import annotations

from pathlib import Path

from onec_analyst.rate_limiter import find_repo_root

REPO_ROOT = find_repo_root(Path(__file__).resolve())
APP_FILE = REPO_ROOT / "app.py"

#: Патчить *до* ``AppTest.from_file``: ``app`` заново импортирует имя из пакета.
PATCH_CHOOSE_CLARIFIED_TEXT = "onec_analyst.clarification.choose_clarified_text"