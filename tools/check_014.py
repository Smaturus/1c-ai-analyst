"""Точечный live-прогон кейса 014 — запускает человек (нужен ключ DeepSeek).

Проверки в отчёте:
(a) fact-check 014 зелёный;
(b) сводка не смешивает интервальное значение с итогом
    («первый интервал 132 000» vs «суммарно 88 000»). Если противоречие
    воспроизводится — НЕ чинить код: зафиксировать в отчёте как кандидат
    на правило сводки (решение — за человеком).

Запуск:
    .\\.venv\\Scripts\\python.exe tools/check_014.py
"""

from __future__ import annotations

import sys
import tempfile
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _entry in (str(_ROOT), str(_ROOT / "src")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

import pandas as pd  # noqa: E402

from onec_analyst import llm_agent  # noqa: E402
from onec_analyst.agent.graph import run_agent_graph  # noqa: E402
from onec_analyst.agent.llm import make_agent_client  # noqa: E402
from onec_analyst.agent.state import CallBudgetGuard, initial_state, status_of  # noqa: E402
from onec_analyst.audit import AuditLog  # noqa: E402
from onec_analyst.pipeline import execute  # noqa: E402
from tests.golden.checks import check_facts  # noqa: E402
from tests.golden.schema import FIXTURE_PATH, load_cases  # noqa: E402


def main() -> int:
    settings = llm_agent.settings_from_env()
    if not settings.is_configured:
        print("LLM не настроена: задайте провайдера/ключ в .env")
        return 1

    case = {c["id"]: c for c in load_cases()}["test_014_filter_product"]
    frame = pd.read_csv(FIXTURE_PATH, parse_dates=["date"])
    profile = {
        "date_min": str(frame["date"].min().date()),
        "date_max": str(frame["date"].max().date()),
    }
    today = date.fromisoformat(case["mock_today"]) if case.get("mock_today") else None
    state = initial_state(
        case["question"],
        previous_dsl=case.get("history_dsl"),
        result_entities=case.get("result_entities"),
        today=today,
    )
    client = make_agent_client(settings)
    with tempfile.TemporaryDirectory(prefix="check-014-") as tmp:
        final = run_agent_graph(
            state,
            client=client,
            settings=settings,
            guard=CallBudgetGuard(3),
            audit=AuditLog(Path(tmp) / "014.jsonl"),
        )
    decision = final.get("decision")
    print("status:", status_of(decision).value if decision is not None else None)
    if decision is None or status_of(decision).value != "ok":
        print("ОЖИДАЛОСЬ ok/timeseries — проверьте decision")
        return 1

    execution = execute(decision, frame, profile)
    print("product_filter after execute:", execution.decision.params.get("product_filter"))
    print("summary:", execution.summary)
    payload = {"values": list(execution.summary_payload.values)}
    try:
        check_facts(execution.summary, case["expected_facts"], payload=payload)
        print("FACT 014: OK")
    except AssertionError as exc:
        print("FACT 014: FAIL:", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
