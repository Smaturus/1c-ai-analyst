"""Проверки формата кассет GoldSet (без replay графа).

Кассета — сырой ответ модели (``responses``) + снимок ``recorded_outcome``.
Проверяется структура: имена инструментов, статусы, наличие полей записи.
Обратная совместимость с одиночным ``response`` сохраняется, но recorded-кассеты
пишутся только в ``responses``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from onec_analyst.llm_agent import INTENT_NAMES, parse_plan_json

from tests.golden.cassette_client import CassetteClient, iter_cassette_paths
from tests.golden.schema import ALLOWED_STATUSES, load_cases

CASSETTE_DIR = Path(__file__).resolve().parent / "golden" / "cassettes"
CASSETTES = iter_cassette_paths(CASSETTE_DIR)
CASE_IDS = {case["id"] for case in load_cases()}

META_TOOLS = {"clarify", "refuse"}


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_cassette_directory_has_example() -> None:
    assert CASSETTES, f"нет кассет в {CASSETTE_DIR}"


@pytest.mark.parametrize("path", CASSETTES, ids=[path.name for path in CASSETTES])
def test_cassette_shape(path: Path) -> None:
    payload = _load(path)
    assert payload["case_id"] in CASE_IDS, payload["case_id"]
    assert payload["source"] in {"recorded", "synthetic_example"}

    client = CassetteClient(payload)
    assert client.case_id == payload["case_id"]

    # Каждая запись — tool_call или content.
    for entry in _responses(payload):
        assert isinstance(entry, dict)
        assert ("tool_call" in entry) != ("content" in entry), entry
        if "content" in entry:
            # content может быть JSON-адаптером (полный DSL), fix_dsl
            # (только аргументы) или resolve_context (resolved_question) —
            # проверяем только, что это непустая строка.
            assert isinstance(entry["content"], str) and entry["content"].strip(), entry
            continue
        call = entry["tool_call"]
        name = call["name"]
        arguments = call.get("arguments") or {}
        if name in INTENT_NAMES:
            parse_plan_json(json.dumps({"intent": name, "params": arguments}))
        elif name in META_TOOLS:
            _check_meta(name, arguments)
        else:
            pytest.fail(f"неизвестный инструмент в кассете: {name!r}")

    # recorded-кассета обязана иметь recorded_outcome и поля записи.
    if payload["source"] == "recorded":
        outcome = payload.get("recorded_outcome") or {}
        assert outcome.get("status") in ALLOWED_STATUSES | {"fallback"}, outcome
        assert "intent" in outcome, outcome
        assert payload.get("provider"), "recorded без provider"
        assert payload.get("model"), "recorded без model"
        assert payload.get("recorded_at"), "recorded без recorded_at"


def _responses(payload: dict) -> list[dict]:
    """Список ответов кассеты (с обратной совместимостью с ``response``)."""
    if isinstance(payload.get("responses"), list):
        return list(payload["responses"])
    if isinstance(payload.get("response"), dict):
        return [payload["response"]]
    return []


def _check_meta(name: str, arguments: dict) -> None:
    if name == "clarify":
        assert isinstance(arguments.get("missing"), list) and arguments["missing"]
        assert isinstance(arguments.get("question"), str) and arguments["question"]
    elif name == "refuse":
        assert isinstance(arguments.get("reason"), str) and arguments["reason"]


def test_synthetic_example_is_marked() -> None:
    """Пример формата не должен выглядеть как запись модели.

    Синтетический пример с одиночным ``response`` хранится под именем
    ``_example_synthetic.json`` (не совпадает с case_id) — это демонстрация
    обратной совместимости; в eval он не попадает.
    """
    example = _load(CASSETTE_DIR / "_example_synthetic.json")
    assert example["source"] == "synthetic_example"
    assert example["provider"] == "example"
    assert "responses" not in example, "синтетический пример — одиночный response"


def test_recorded_entries_use_responses_not_singular_response() -> None:
    """recorded-кассеты пишутся в ``responses``; одиночный ``response`` — legacy."""
    for path in CASSETTES:
        payload = _load(path)
        if payload["source"] != "recorded":
            continue
        assert isinstance(payload.get("responses"), list), path.name
