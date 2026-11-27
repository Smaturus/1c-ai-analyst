"""Проверки нормализованного GoldSet 6.4.

Это ещё не replay модели: тесты следят за целостностью набора — канонический
DSL, статусы, факты, периоды, отсутствие обозначений 6.3 и покрытие фикстуры.
"""

from __future__ import annotations

from collections import Counter
from datetime import date

import pandas as pd
import pytest

from onec_analyst.analytics import INTENT_PARAMETERS
from onec_analyst.llm_agent import INTENT_NAMES

from tests.golden.schema import (
    CASES_PATH,
    FIXTURE_PATH,
    GENERIC_ITEMS,
    LEGACY_KEYS,
    canonical_params,
    load_cases,
    load_document,
    period_pairs,
    validate_case,
    walk_keys,
)

CASES = load_cases()
CASES_BY_ID = {case["id"]: case for case in CASES}


def test_document_version_is_64() -> None:
    assert load_document()["version"] == "6.4"


def test_has_50_unique_cases() -> None:
    assert len(CASES) == 50
    assert len(CASES_BY_ID) == 50
    assert CASES_PATH.is_file()


def test_all_cases_pass_schema_validation() -> None:
    errors = {
        case["id"]: validate_case(case)
        for case in CASES
        if validate_case(case)
    }
    assert not errors, errors


def test_status_distribution_is_frozen() -> None:
    counts = Counter(case["expected_status"] for case in CASES)
    assert counts == {"ok": 31, "clarify": 6, "refuse": 13}


def test_all_seven_intents_are_covered_by_ok_cases() -> None:
    intents = {
        case["expected_intent"]
        for case in CASES
        if case["expected_status"] == "ok"
    }
    assert intents == set(INTENT_NAMES)


def test_no_legacy_keys_anywhere() -> None:
    for case in CASES:
        legacy = sorted(set(walk_keys(case)) & LEGACY_KEYS)
        assert not legacy, f"{case['id']}: устаревшие ключи {legacy}"


def test_result_entities_are_outside_dsl() -> None:
    for case in CASES:
        for source in ("expected_dsl", "history_dsl"):
            dsl = case.get(source)
            if isinstance(dsl, dict):
                assert "result_entities" not in dsl, f"{case['id']}: {source}"


def test_period_parameters_are_pairs() -> None:
    for case in CASES:
        for key, value in period_pairs(case):
            assert isinstance(value, list) and len(value) == 2, (
                f"{case['id']}: {key} должно быть парой дат, получено {value!r}"
            )
            start, end = (date.fromisoformat(item) for item in value)
            assert start <= end, f"{case['id']}: {key} начало позже конца"


def test_filters_are_strings() -> None:
    for case in CASES:
        for source in ("expected_dsl", "history_dsl"):
            dsl = case.get(source)
            if not isinstance(dsl, dict):
                continue
            params = dsl.get("params") or {}
            for key in ("product_filter", "group_filter"):
                if key in params:
                    assert isinstance(params[key], str), (
                        f"{case['id']}: {source}.{key} должен быть строкой"
                    )


def test_context_cases_have_history_or_entities() -> None:
    for case in CASES:
        if case["category"] != "context":
            continue
        assert case.get("history_dsl") or case.get("result_entities"), case["id"]


def test_no_xfail_cases_remain() -> None:
    """008/033 — честный refuse без xfail; иначе XPASS(strict) в replay."""
    xfail = {case["id"] for case in CASES if case.get("xfail")}
    assert xfail == set()


def test_reclassified_cases_match_adr() -> None:
    assert CASES_BY_ID["test_008_memory_pronoun"]["expected_status"] == "refuse"
    assert CASES_BY_ID["test_023_l4l_specific"]["expected_status"] == "refuse"
    assert CASES_BY_ID["test_029_context_pronoun_products"]["expected_status"] == "refuse"
    assert CASES_BY_ID["test_009_memory_ellipsis"]["expected_status"] == "clarify"
    assert CASES_BY_ID["test_016_context_change_intent"]["expected_status"] == "refuse"


def test_default_limit_is_5() -> None:
    """Канон S1: дефолт limit = 5 (DECISIONS.md#1); 036 — без числа."""
    case = CASES_BY_ID["test_036_default_limit"]
    _, params = canonical_params(case["expected_dsl"])
    assert params["limit"] == 5


def test_value_facts_have_numbers_for_subset() -> None:
    """10–12 ok-кейсов имеют числовые value-факты (константы на фикстуре).

    Числа посчитаны независимым pandas-запросом по ``golden_sales.csv`` (не через
    ``dispatch_intent``), чтобы факт-чек не сравнивал расчёт с самим собой.
    Проверка самих чисел — в ``check_facts`` через ``SummaryPayload``.
    """
    value_facts = [
        fact
        for case in CASES
        for fact in case["expected_facts"]
        if fact["type"] == "value" and "value" in fact
    ]
    assert 10 <= len(value_facts) <= 12, len(value_facts)
    for fact in value_facts:
        assert isinstance(fact["value"], (int, float)), fact


def test_fixture_covers_concrete_entities() -> None:
    frame = pd.read_csv(FIXTURE_PATH)
    known = set(frame["product"]) | set(frame["product_group"])
    missing: list[tuple[str, str]] = []
    for case in CASES:
        if case["expected_status"] != "ok":
            continue
        for fact in case["expected_facts"]:
            if fact["type"] != "value":
                continue
            item = fact["item"]
            if item in GENERIC_ITEMS:
                continue
            if item not in known:
                missing.append((case["id"], item))
    assert not missing, f"фикстура не покрывает сущности: {missing}"


def test_fixture_has_negative_rows_and_three_years() -> None:
    frame = pd.read_csv(FIXTURE_PATH, parse_dates=["date"])
    assert frame["revenue"].lt(0).any()
    assert frame["date"].dt.year.min() == 2022
    assert frame["date"].dt.year.max() == 2024
    assert {"Электроника", "Аксессуары", "Продукты"} <= set(frame["product_group"])


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_case_can_be_normalized(case: dict) -> None:
    """Каждый ok-кейс проходит Pydantic и даёт параметры канона."""
    if case["expected_status"] != "ok":
        return
    intent, params = canonical_params(case["expected_dsl"])
    assert intent == case["expected_intent"]
    assert set(params) <= set(INTENT_PARAMETERS[intent])
