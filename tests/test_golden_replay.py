"""Replay кассет GoldSet в CI: детерминированная часть пайплайна без сети.

Кассета хранит сырые ответы модели и ``recorded_outcome`` (снимок status/intent,
что граф сделал при записи). Replay воспроизводит граф через
:class:`CassetteClient` и проверяет, что результат совпал с ``recorded_outcome``.

Это **не** оценка качества модели: сравнение с ``expected_dsl``/``expected_status``
остаётся в ``test_golden_expected_dsl.py`` и ``tools/eval_live.py``. Replay ловит
регрессии парсера, Pydantic, ``fix_dsl``, маршрутизации и статусов.

Для ok-кейсов дополнительно исполняется ``pipeline.execute`` на фикстуре:
расчёт не должен падать, а сводка/payload — быть непустыми. Сеть не используется.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from onec_analyst.agent.graph import run_agent_graph
from onec_analyst.agent.state import CallBudgetGuard, initial_state, status_of
from onec_analyst.audit import AuditLog
from onec_analyst.pipeline import execute

from tests.golden.cassette_client import CassetteClient
from tests.golden.schema import FIXTURE_PATH, load_cases

CASSETTE_DIR = Path(__file__).resolve().parent / "golden" / "cassettes"

CASES = load_cases()


def _initial_state_for(case: dict) -> dict:
    """Тот же ``initial_state``, что строил ``tools/record_cassettes.py``."""
    today = None
    if case.get("mock_today"):
        today = date.fromisoformat(str(case["mock_today"]))
    return initial_state(
        case["question"],
        previous_dsl=case.get("history_dsl"),
        result_entities=case.get("result_entities"),
        today=today,
    )


@pytest.fixture(scope="module")
def golden_frame() -> pd.DataFrame:
    return pd.read_csv(FIXTURE_PATH, parse_dates=["date"])


@pytest.fixture(scope="module")
def profile(golden_frame: pd.DataFrame) -> dict[str, str]:
    return {
        "date_min": str(golden_frame["date"].min().date()),
        "date_max": str(golden_frame["date"].max().date()),
    }


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_replay_reproduces_recorded_outcome(
    case: dict,
    golden_frame: pd.DataFrame,
    profile: dict[str, str],
    tmp_path: Path,
) -> None:
    case_id = case["id"]
    if case.get("xfail"):
        pytest.skip(case.get("xfail_reason") or "xfail")

    path = CASSETTE_DIR / f"{case_id}.json"
    if not path.is_file():
        pytest.fail(
            f"Нет кассеты {path.name}. Запишите её: "
            f"python tools/record_cassettes.py --case {case_id}"
        )

    client = CassetteClient.load(path)
    final = run_agent_graph(
        _initial_state_for(case),
        client=client,
        guard=CallBudgetGuard(3),
        audit=AuditLog(tmp_path / f"{case_id}.jsonl"),
    )

    decision = final.get("decision")
    status = status_of(decision).value
    outcome = client.recorded_outcome
    assert status == outcome.get("status"), (
        f"{case_id}: status {status!r} != recorded {outcome.get('status')!r}"
    )
    actual_intent = decision.intent if decision is not None else None
    assert actual_intent == outcome.get("intent"), (
        f"{case_id}: intent {actual_intent!r} != recorded {outcome.get('intent')!r}"
    )

    if status == "ok":
        assert decision is not None and decision.intent, f"{case_id}: ok без интента"
        execution = execute(decision, golden_frame, profile)
        assert execution.summary_payload is not None, f"{case_id}: пустой payload"
        assert execution.summary.strip(), f"{case_id}: пустая сводка"
    elif status == "clarify":
        assert decision is not None and decision.clarification is not None
        assert decision.clarification.missing, f"{case_id}: clarify без missing"
    elif status == "refuse":
        assert decision is not None and decision.refusal_reason, (
            f"{case_id}: refuse без refusal_reason"
        )
