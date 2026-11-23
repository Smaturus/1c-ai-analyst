"""GoldSet на expected DSL: pipeline + факт-чек без LLM и без графа.

Проверяет, что канонический ожидаемый DSL исполняется на фикстуре, а сводка
содержит все ярлыки из expected_facts. Это защита от ситуации, когда граф
обвиняют в провале, который на самом деле вызван форматом сводки.
"""

from __future__ import annotations

import pandas as pd
import pytest

from onec_analyst.pipeline import execute, validate_decision

from tests.golden.checks import check_facts
from tests.golden.schema import FIXTURE_PATH, load_cases

OK_CASES = [case for case in load_cases() if case["expected_status"] == "ok"]


@pytest.fixture(scope="module")
def golden_frame() -> pd.DataFrame:
    frame = pd.read_csv(FIXTURE_PATH, parse_dates=["date"])
    return frame


@pytest.fixture(scope="module")
def profile(golden_frame: pd.DataFrame) -> dict[str, str]:
    return {
        "date_min": str(golden_frame["date"].min().date()),
        "date_max": str(golden_frame["date"].max().date()),
    }


@pytest.mark.parametrize("case", OK_CASES, ids=[case["id"] for case in OK_CASES])
def test_expected_dsl_executes_and_facts_match(
    case: dict,
    golden_frame: pd.DataFrame,
    profile: dict[str, str],
) -> None:
    decision = validate_decision(case["expected_dsl"])
    assert decision.intent == case["expected_intent"]

    execution = execute(decision, golden_frame, profile)
    payload = {"values": list(execution.summary_payload.values)}

    check_facts(
        execution.summary,
        case["expected_facts"],
        payload=payload,
    )
