"""Изоляция ядра от Streamlit: core-модули не тянут UI.

Проверяются два независимых свойства:

* AST-импорты ``dispatcher``/``pipeline``/``summary``/``session`` не содержат
  ``streamlit`` и ``app`` (иначе рефакторинг вернёт монолит);
* импорт этих модулей в чистом процессе **не** кладёт ``streamlit`` в
  ``sys.modules``.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

from tests.paths import REPO_ROOT

CORE_MODULES = ("dispatcher", "pipeline", "summary", "session")
CORE_DIR = REPO_ROOT / "src" / "onec_analyst"


def _imported_names(path: Path) -> set[str]:
    """Все имена из ``import X``/``from X import ...`` в файле."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_core_sources_do_not_import_streamlit_or_app() -> None:
    offenders: dict[str, set[str]] = {}
    for module in CORE_MODULES:
        imported = _imported_names(CORE_DIR / f"{module}.py")
        forbidden = imported & {"streamlit", "app", "app_local"}
        if forbidden:
            offenders[module] = forbidden

    assert offenders == {}, f"core-модули импортируют UI: {offenders}"


def test_importing_core_does_not_load_streamlit() -> None:
    code = (
        "import sys\n"
        "import onec_analyst.dispatcher\n"
        "import onec_analyst.pipeline\n"
        "import onec_analyst.summary\n"
        "import onec_analyst.session\n"
        "assert 'streamlit' not in sys.modules, 'streamlit загружен ядром'\n"
        "assert 'app' not in sys.modules, 'app загружен ядром'\n"
        "print('ok')\n"
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT / "src"), str(REPO_ROOT), environment.get("PYTHONPATH", "")]
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert "ok" in completed.stdout
