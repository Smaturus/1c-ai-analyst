"""Изоляция агента: без Streamlit и без вызова графа в офлайн-режиме."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from onec_analyst.agent import graph as agent_graph
from onec_analyst.dispatcher import process_request
from onec_analyst.llm_agent import LLMSettings
from onec_analyst.session import ConversationState
from tests.paths import REPO_ROOT

AGENT_DIR = REPO_ROOT / "src" / "onec_analyst" / "agent"
PROFILE = {"date_min": "2023-01-15", "date_max": "2024-03-05"}


def _imported_names(path: Path) -> set[str]:
    """Все корневые имена из ``import X``/``from X import ...``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_agent_sources_do_not_import_streamlit_or_app() -> None:
    """Ни один модуль agent/* не тянет UI."""
    offenders: dict[str, set[str]] = {}
    for path in sorted(AGENT_DIR.glob("*.py")):
        forbidden = _imported_names(path) & {"streamlit", "app", "app_local"}
        if forbidden:
            offenders[path.name] = forbidden

    assert offenders == {}, f"agent импортирует UI: {offenders}"


def test_importing_agent_does_not_load_streamlit() -> None:
    """Импорт графа в чистом процессе не кладёт streamlit в ``sys.modules``."""
    code = (
        "import sys\n"
        "import onec_analyst.agent.graph\n"
        "import onec_analyst.agent.nodes\n"
        "assert 'streamlit' not in sys.modules, 'streamlit загружен агентом'\n"
        "assert 'app' not in sys.modules, 'app загружен агентом'\n"
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


def test_offline_request_does_not_call_graph(
    monkeypatch: pytest.MonkeyPatch, sales_frame: pd.DataFrame
) -> None:
    """``use_llm=False``: граф не вызывается даже при настроенном провайдере."""

    def _fail(*args, **kwargs):
        raise AssertionError("граф вызван в офлайн-режиме")

    monkeypatch.setattr(agent_graph, "run_agent_graph", _fail)
    settings = LLMSettings(provider="deepseek", model="deepseek-chat", api_key="test-key")

    outcome = process_request(
        "Итог за 2024",
        False,
        ConversationState(),
        sales_frame,
        PROFILE,
        settings=settings,
    )

    assert outcome.ok is True
    assert outcome.source == "rules"


def test_agent_path_is_the_only_graph_caller() -> None:
    """Диспетчер импортирует граф лениво и только в агентной ветке."""
    source = (REPO_ROOT / "src" / "onec_analyst" / "dispatcher.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    top_level_imports = {
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module
    }

    assert "onec_analyst.agent.graph" not in top_level_imports
    assert "run_agent_graph" in source
