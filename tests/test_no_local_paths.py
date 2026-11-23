"""Страж: в отслеживаемых файлах нет локальных путей и имён рабочей машины.

Правило (глобальные AGENTS.md, «Никаких локальных путей в публикации»):
абсолютные пути, имена пользователей/машин/рабочих каталогов и папок проектов
не должны попадать в репозиторий. Такой текст невозможно «отозвать» после
публикации, поэтому проверка стоит в CI и падает до коммита.

Проверяются:

* абсолютные пути Windows (`D:\\...`), кроме вымышленных заглушек и стандартных
  путей установки (`C:\\Program Files`, `C:\\ProgramData`);
* личные каталоги Unix (`/Users/...`, `/home/...`);
* UNC-пути (`\\\\server\\share`).

Имена конкретных рабочих каталогов в самом страже **не хранятся** — иначе они
снова попали бы в публикацию. Они читаются из локального `.local_paths.txt`
(в `.gitignore`): по строке на шаблон, `#` — комментарий. В CI файла нет, и
базовые проверки работают без него.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Локальный список имён/шаблонов, которых не должно быть в публикации.
LOCAL_DENYLIST = REPO_ROOT / ".local_paths.txt"

#: Абсолютный путь Windows вида ``D:\\...`` (но не экранирование в регулярках).
DRIVE_PATH = re.compile(r"(?<![\\A-Za-z])[A-Za-z]:\\")

#: Личные каталоги Unix.
HOME_PATHS = re.compile(r"/Users/[A-Za-z]|/home/[a-z]")

#: UNC-путь к сетевой шаре.
UNC_PATH = re.compile(r"\\\\[A-Za-z0-9._-]+\\")

#: Что считается заглушкой или стандартным путём установки.
ALLOWED = re.compile(
    r"C:\\path\\to|C:\\Program Files|C:\\ProgramData|C:\\certs|C:\\Windows|"
    r"C:/path/to|/path/to|\\\\path\\\\|<[^>]+>"
)

#: Строки, которые проверяют ОТСУТСТВИЕ путей, а не содержат их.
ABSENCE_CHECK = re.compile(r"not in (text|body|page|payload|message)")

#: Бинарные/тяжёлые форматы не читаем как текст.
SKIP_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    ".ico",
    ".pdf",
    ".xlsx",
    ".xls",
    ".zip",
    ".whl",
    ".db",
}

#: Сам страж: его шаблоны содержат образцы путей и не являются утечкой.
SKIP_FILES = {"tests/test_no_local_paths.py"}


def _tracked_files() -> list[str]:
    """Файлы под контролем версий (``git ls-files``)."""
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def local_tokens() -> list[str]:
    """Имена из локального денилиста (в CI файла нет — список пуст)."""
    if not LOCAL_DENYLIST.is_file():
        return []
    tokens: list[str] = []
    for line in LOCAL_DENYLIST.read_text(encoding="utf-8").splitlines():
        token = line.strip()
        if token and not token.startswith("#"):
            tokens.append(token.lower())
    return tokens


def _violations() -> list[str]:
    """Найти строки с локальными путями или именами рабочей машины."""
    tokens = local_tokens()
    problems: list[str] = []
    for name in _tracked_files():
        if name in SKIP_FILES:
            continue
        path = REPO_ROOT / name
        if not path.is_file() or path.suffix.lower() in SKIP_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:  # нечитаемый файл — не наша забота
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            # В исходниках обратный слэш бывает удвоен (строковые литералы):
            # для проверки приводим к обычному виду.
            normalized = line.replace("\\\\", "\\")
            if ALLOWED.search(normalized) or ABSENCE_CHECK.search(normalized):
                continue
            lowered = normalized.lower()
            # Токены из денилиста ищем по границам слова: иначе имя вида
            # «<пользователь>» совпало бы с публичным «Smaturus» в ссылках GitHub.
            token_hit = any(
                re.search(
                    rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", lowered
                )
                for token in tokens
            )
            hit = (
                DRIVE_PATH.search(normalized)
                or HOME_PATHS.search(normalized)
                or UNC_PATH.search(normalized)
                or token_hit
            )
            if hit:
                problems.append(f"{name}:{number}: {line.strip()[:120]}")
    return problems


def test_no_local_paths_in_tracked_files() -> None:
    """Ни один отслеживаемый файл не содержит локальных путей рабочей машины."""
    problems = _violations()

    assert problems == [], (
        "Локальные пути в публикуемых файлах недопустимы "
        "(глобальные AGENTS.md). Замените абсолютный путь на относительный "
        "или заглушку <путь>:\n" + "\n".join(problems)
    )
