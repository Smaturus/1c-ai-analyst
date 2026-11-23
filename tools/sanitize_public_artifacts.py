"""Санитизация публичных артефактов.

Идемпотентен: повторный запуск ничего не ломает. Заменяет фингерпринты
непубличной базы на заглушки, реальную номенклатуру — на синтетическую.

Фингерпринты (имена, суммы, объёмы) в скрипте **не хранятся** — иначе они
снова попали бы в публикацию. Они читаются из локального
``.private_markers.json`` (в ``.gitignore``), структура::

    {
      "forbidden": [{"label": "...", "regex": "..."}],
      "nom": {"реальное имя": "синтетическая замена"},
      "amounts": ["<сумма>", "..."],
      "counts": ["17610", "..."]
    }

    python tools/sanitize_public_artifacts.py          # применить
    python tools/sanitize_public_artifacts.py --check   # только отчёт (для CI)
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RULES_FILE = ROOT / ".private_markers.json"
CHECK = "--check" in sys.argv

TEXT = {".md", ".py", ".json", ".csv", ".txt", ".yml", ".yaml", ".example"}
SKIP = {"tests/test_public_figures.py", "tools/sanitize_public_artifacts.py"}


def _rx(digits: str, tail: str = "") -> re.Pattern[str]:
    body = r"[\s\u00a0,.]*".join(digits)
    return re.compile(r"(?<!\d)" + body + r"(?!\d)" + tail)


def _load_rules() -> tuple[dict[str, str], list[tuple[re.Pattern[str], str]]]:
    """Правила замены из локального файла; без файла правил нет."""
    if not RULES_FILE.is_file():
        return {}, []
    data = json.loads(RULES_FILE.read_text(encoding="utf-8"))
    nom = {str(old): str(new) for old, new in data.get("nom", {}).items()}
    rules: list[tuple[re.Pattern[str], str]] = []
    for item in data.get("forbidden", []):
        if "replacement" not in item:
            continue
        rules.append(
            (re.compile(str(item["regex"]), re.IGNORECASE), str(item["replacement"]))
        )
    rules += [
        (_rx(str(d), r"(?:[.,]\d{2})?"), "<сумма>") for d in data.get("amounts", [])
    ]
    rules += [
        (_rx(str(d), r"(?=\s*(запис|строк|позици|шт|продаж|rows))"), "<N>")
        for d in data.get("counts", [])
    ]
    return nom, rules


NOM, RULES = _load_rules()


def sanitize(text: str) -> str:
    for old, new in NOM.items():
        text = text.replace(old, new)
    for pattern, replacement in RULES:
        text = pattern.sub(replacement, text)
    return text


def main() -> int:
    if not RULES_FILE.is_file():
        print("Правила не загружены: нет .private_markers.json (в CI это ожидаемо).")
    changed: list[str] = []
    files = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    for name in files:
        path = ROOT / name
        if name in SKIP or path.suffix not in TEXT or not path.is_file():
            continue
        old = path.read_text(encoding="utf-8")
        new = sanitize(old)
        if new != old:
            changed.append(name)
            if not CHECK:
                path.write_text(new, encoding="utf-8")
    print(("Требуют правки: " if CHECK else "Изменено: ") + str(len(changed)))
    for name in changed:
        print("  ", name)
    return 1 if CHECK and changed else 0


if __name__ == "__main__":
    raise SystemExit(main())
