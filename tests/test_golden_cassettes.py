"""Проверки формата кассет GoldSet (без replay графа)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from onec_analyst.llm_agent import INTENT_NAMES, parse_plan_json

from tests.golden.schema import load_cases

CASSETTE_DIR = Path(__file__).resolve().parent / "golden" / "cassettes"
CASSETTES = sorted(CASSETTE_DIR.glob("*.json"))
CASE_IDS = {case["id"] for case in load_cases()}


def test_cassette_directory_has_example() -> None:
    assert CASSETTES, f"нет кассет в {CASSETTE_DIR}"


@pytest.mark.parametrize("path", CASSETTES, ids=[path.name for path in CASSETTES])
def test_cassette_shape(path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["case_id"] in CASE_IDS, payload["case_id"]
    assert payload["source"] in {"recorded", "synthetic_example"}
    response = payload["response"]
    assert isinstance(response, dict) and ("tool_call" in response or "content" in response)

    if "content" in response:
        parse_plan_json(str(response["content"]))
        return

    call = response["tool_call"]
    name = call["name"]
    arguments = call.get("arguments") or {}
    if name in INTENT_NAMES:
        parse_plan_json(json.dumps({"intent": name, "params": arguments}))
    elif name == "clarify":
        assert isinstance(arguments.get("missing"), list) and arguments["missing"]
        assert isinstance(arguments.get("question"), str) and arguments["question"]
    elif name == "refuse":
        assert isinstance(arguments.get("reason"), str) and arguments["reason"]
    else:
        pytest.fail(f"неизвестный инструмент в кассете: {name!r}")


def test_synthetic_example_is_marked() -> None:
    """Пример формата не должен выглядеть как запись модели."""
    example = json.loads((CASSETTE_DIR / "test_001_top_simple.json").read_text(encoding="utf-8"))
    assert example["source"] == "synthetic_example"
    assert example["provider"] == "example"
