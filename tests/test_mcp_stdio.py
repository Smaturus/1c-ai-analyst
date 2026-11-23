"""Проверка, что сервер реально поднимается как MCP-процесс по stdio.

В отличие от тестов прямых вызовов, здесь поднимается отдельный процесс
``python -m onec_analyst.mcp_server``, выполняется MCP-handshake, перечисление
инструментов и вызовы срезов. Это тот же путь, которым пользуется Hermes.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp")

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
TIMEOUT_SECONDS = 60.0


def _server_params() -> StdioServerParameters:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    env["PYTHONUTF8"] = "1"
    env["ONEC_MCP_AUDIT_FILE"] = str(REPO_ROOT / ".pytest_tmp" / "stdio_audit.jsonl")
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "onec_analyst.mcp_server"],
        cwd=str(REPO_ROOT),
        env=env,
    )


def _text(result: Any) -> str:
    return "\n".join(block.text for block in result.content if block.type == "text")


async def _scenario() -> dict[str, Any]:
    with open(os.devnull, "w", encoding="utf-8") as errlog:
        async with stdio_client(_server_params(), errlog=errlog) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                total = await session.call_tool(
                    "total",
                    {"start_date": "2024-01-01", "end_date": "2024-12-31"},
                )
                alert = await session.call_tool("returns_alert", {"today": "2023-02-16"})
                refused = await session.call_tool(
                    "load_dataset",
                    {"path": "C:/Windows/win.ini"},
                )
                return {
                    "tools": [tool.name for tool in listed.tools],
                    "total": _text(total),
                    "alert": _text(alert),
                    "refused": _text(refused),
                }


@pytest.fixture(scope="module")
def stdio_result() -> dict[str, Any]:
    return asyncio.run(asyncio.wait_for(_scenario(), TIMEOUT_SECONDS))


def test_handshake_lists_typed_tools(stdio_result: dict[str, Any]) -> None:
    names = stdio_result["tools"]

    assert {"total", "top_bottom_products", "group_dynamics", "returns_alert"} <= set(names)
    assert "execute_dsl" not in names
    assert len(names) >= 11


def test_slice_over_stdio_matches_expected_numbers(stdio_result: dict[str, Any]) -> None:
    text = stdio_result["total"]

    assert "Период: 01.01.2024–31.12.2024" in text
    assert "Данные:" in text
    assert "52 253 500,00" in text


def test_alert_over_stdio(stdio_result: dict[str, Any]) -> None:
    text = stdio_result["alert"]

    assert "⚠️ Алерт" in text
    assert "350 000,00" in text


def test_outside_path_is_refused_over_stdio(stdio_result: dict[str, Any]) -> None:
    assert "вне каталога проекта" in stdio_result["refused"]
