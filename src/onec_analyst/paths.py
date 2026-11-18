"""Пути проекта, не зависящие от глубины вложенности модулей пакета.

Корень репозитория ищется по маркерам (``pyproject.toml`` / ``.git``), а не
через ``Path(__file__).parent`` или ``parents[N]``: после переноса модулей в
``src/onec_analyst/`` такие пути молча указывали внутрь пакета.
"""

from __future__ import annotations

from pathlib import Path


def find_repo_root(start: Path | None = None) -> Path:
    """Найти корень репозитория по маркерам ``pyproject.toml`` или ``.git``.

    Обходит каталоги вверх от ``start`` (по умолчанию — файл этого модуля).
    Если маркер не найден — возвращает ближайший разумный fallback (родитель
    ``src/`` или cwd).
    """
    current = (start or Path(__file__).resolve()).parent
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").is_file() or (candidate / ".git").exists():
            return candidate
    # Установленный wheel без pyproject рядом: src/onec_analyst → … или cwd
    if current.name == "onec_analyst" and current.parent.name == "src":
        return current.parent.parent
    return Path.cwd()


__all__ = ["find_repo_root"]
