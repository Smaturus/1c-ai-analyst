"""Live-прогон GoldSet на живой модели: DSL-точность и факт-чек (не в CI).

Прогоняет 50 кейсов через реальный граф (не кассеты), провайдер/модель из
окружения, и измеряет качество модели относительно `expected_status`/
`expected_dsl` из `tests/golden/cases.json`. Это единственное место, где
оценивается модель (replay — только регрессия пайплайна).

Метрики:
- status accuracy — совпал ли статус с ожидаемым;
- DSL accuracy — для ok: совпали ли интент и канонические параметры;
- fact-check — для ok: `check_facts` по `SummaryPayload` (все ярлыки + числа).

Отчёт пишется в Markdown (`docs/EVAL_LIVE.md`) и JSON (`docs/EVAL_LIVE.json`).
``test_033`` (revenue_vat вне MVP) пропускается по xfail.

Использование::

    python tools/eval_live.py                     # провайдер из .env
    python tools/eval_live.py --provider gigachat --model GigaChat-2-Pro
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
for _entry in (str(_ROOT), str(_ROOT / "src")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

import pandas as pd  # noqa: E402

from onec_analyst import llm_agent  # noqa: E402
from onec_analyst.agent.graph import run_agent_graph  # noqa: E402
from onec_analyst.agent.llm import make_agent_client  # noqa: E402
from onec_analyst.agent.state import (  # noqa: E402
    AgentState,
    CallBudgetGuard,
    initial_state,
    status_of,
)
from onec_analyst.audit import AuditLog  # noqa: E402
from onec_analyst.data_loader import PRODUCT  # noqa: E402
from onec_analyst.entities import resolve_entity  # noqa: E402
from onec_analyst.pipeline import execute  # noqa: E402
from tests.golden.checks import check_facts  # noqa: E402
from tests.golden.schema import FIXTURE_PATH, canonical_params, load_cases  # noqa: E402

DOCS_DIR = _ROOT / "docs"
LIVE_MD = DOCS_DIR / "EVAL_LIVE.md"
LIVE_JSON = DOCS_DIR / "EVAL_LIVE.json"

CATEGORIES = ("simple", "context", "edge_case", "non_analytical")


def _json_safe(value: Any) -> Any:
    """Сериализовать значения DSL (date/Timestamp → строка) для сравнения."""
    return json.loads(json.dumps(value, default=str))


def _resolve_filters(params: dict[str, Any], frame: pd.DataFrame) -> dict[str, Any]:
    """Привести ``product_filter``/``group_filter`` к каноническому виду каталога.

    Резолвер — часть пайплайна, поэтому итоговые параметры системы каноничны;
    сравнение сырого выхода LLM с эталоном сравнивало бы промежуточное состояние.
    """
    resolved = dict(params)
    for key, column in (("product_filter", PRODUCT), ("group_filter", "product_group")):
        value = resolved.get(key)
        if isinstance(value, str) and value.strip() and column in frame.columns:
            catalog = frame[column].dropna().astype("string").unique().tolist()
            resolved[key] = resolve_entity(value.strip(), catalog).value
    return resolved


def build_state(case: dict[str, Any]) -> AgentState:
    today = None
    if case.get("mock_today"):
        today = date.fromisoformat(str(case["mock_today"]))
    return initial_state(
        case["question"],
        previous_dsl=case.get("history_dsl"),
        result_entities=case.get("result_entities"),
        today=today,
    )


def evaluate_case(
    case: dict[str, Any],
    client: Any,
    settings: Any,
    frame: pd.DataFrame,
    profile: dict[str, str],
    audit_dir: Path,
) -> dict[str, Any]:
    """Прогнать кейс через граф и сравнить с ожиданиями GoldSet."""
    case_id = case["id"]
    if case.get("xfail"):
        return {"case_id": case_id, "skipped": True, "reason": case.get("xfail_reason")}

    final = run_agent_graph(
        build_state(case),
        client=client,
        settings=settings,
        guard=CallBudgetGuard(3),
        audit=AuditLog(audit_dir / f"{case_id}.jsonl"),
    )
    decision = final.get("decision")
    status = status_of(decision).value
    expected_status = case["expected_status"]

    result: dict[str, Any] = {
        "case_id": case_id,
        "category": case["category"],
        "status": status,
        "expected_status": expected_status,
        "intent": decision.intent if decision is not None else None,
        "skipped": False,
    }

    if expected_status == "ok":
        result["expected_intent"] = case["expected_intent"]
        if status != "ok":
            result["status_match"] = False
            result["dsl_match"] = False
            result["fact_match"] = False
            return result
        assert decision is not None  # status=ok ⇒ решение есть
        exp_intent, exp_params = canonical_params(case["expected_dsl"])
        exp_params = _resolve_filters(exp_params, frame)
        actual_params = _resolve_filters(dict(decision.params or {}), frame)
        intent_match = decision.intent == exp_intent
        params_match = _json_safe(actual_params) == _json_safe(exp_params)
        result["status_match"] = True
        result["intent_match"] = intent_match
        result["params_match"] = params_match
        result["dsl_match"] = intent_match and params_match
        result["expected_params"] = _json_safe(exp_params)
        result["actual_params"] = _json_safe(actual_params)

        fact_match = False
        fact_error = None
        if result["dsl_match"]:
            try:
                execution = execute(decision, frame, profile)
                payload = {"values": list(execution.summary_payload.values)}
                check_facts(execution.summary, case["expected_facts"], payload=payload)
                fact_match = True
            except Exception as exc:
                fact_error = f"{type(exc).__name__}: {exc}"
        result["fact_match"] = fact_match
        result["fact_error"] = fact_error
        return result

    # clarify / refuse / error: сравниваем только статус.
    result["status_match"] = status == expected_status
    result["dsl_match"] = result["status_match"]
    return result


def _pct(num: int, den: int) -> float:
    return round(100.0 * num / den, 1) if den else 0.0


def run_all(
    settings: Any,
    *,
    cases: list[dict[str, Any]],
    frame: pd.DataFrame,
    profile: dict[str, str],
    audit_dir: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    client = make_agent_client(settings)
    results = [evaluate_case(case, client, settings, frame, profile, audit_dir) for case in cases]

    active = [r for r in results if not r.get("skipped")]
    by_cat: dict[str, list[dict[str, Any]]] = {cat: [] for cat in CATEGORIES}
    for r in active:
        by_cat[r["category"]].append(r)

    def acc(rows: list[dict[str, Any]], key: str) -> float:
        total = len(rows)
        if not total:
            return 0.0
        return _pct(sum(1 for r in rows if r.get(key)), total)

    ok_rows = [r for r in active if r.get("expected_status") == "ok"]
    fact_total = sum(1 for r in ok_rows if r.get("dsl_match"))
    fact_hits = sum(1 for r in ok_rows if r.get("fact_match"))

    metrics = {
        "provider": settings.provider,
        "model": settings.model,
        "date": date.today().isoformat(),
        "total": len(active),
        "skipped": sum(1 for r in results if r.get("skipped")),
        "status_accuracy": acc(active, "status_match"),
        "dsl_accuracy": acc(active, "dsl_match"),
        "fact_check": {
            "executed": fact_total,
            "passed": fact_hits,
            "accuracy": _pct(fact_hits, fact_total),
        },
        "by_category": {
            cat: {
                "n": len(by_cat[cat]),
                "status_accuracy": acc(by_cat[cat], "status_match"),
                "dsl_accuracy": acc(by_cat[cat], "dsl_match"),
            }
            for cat in CATEGORIES
        },
    }
    return results, metrics


def render_markdown(results: list[dict[str, Any]], metrics: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# EVAL Live")
    lines.append("")
    lines.append(
        f"- Провайдер/модель: **{metrics['provider']} / {metrics['model']}**"
    )
    lines.append(f"- Дата прогона: {metrics['date']}")
    lines.append(
        f"- Кейсов: {metrics['total']} (пропущено по xfail: {metrics['skipped']})"
    )
    lines.append("")
    lines.append("## Метрики")
    lines.append("")
    lines.append("| Метрика | Значение |")
    lines.append("|---|---|")
    lines.append(f"| Status accuracy | {metrics['status_accuracy']}% |")
    lines.append(f"| DSL accuracy | {metrics['dsl_accuracy']}% |")
    fc = metrics["fact_check"]
    lines.append(
        f"| Fact-check | {fc['accuracy']}% ({fc['passed']}/{fc['executed']}) |"
    )
    lines.append("")
    lines.append("## По категориям")
    lines.append("")
    lines.append("| Категория | N | Status | DSL |")
    lines.append("|---|---|---|---|")
    for cat in CATEGORIES:
        c = metrics["by_category"][cat]
        lines.append(
            f"| {cat} | {c['n']} | {c['status_accuracy']}% | {c['dsl_accuracy']}% |"
        )
    lines.append("")

    mismatches = [
        r
        for r in results
        if not r.get("skipped") and not r.get("dsl_match")
    ]
    lines.append(f"## Расхождения с GoldSet ({len(mismatches)})")
    lines.append("")
    if mismatches:
        lines.append("| Кейс | Категория | Ожидалось | Получено |")
        lines.append("|---|---|---|---|")
        for r in mismatches:
            got = r.get("status") if r.get("status") != "ok" else r.get("intent")
            lines.append(
                f"| {r['case_id']} | {r['category']} | "
                f"{r.get('expected_status')}"
                f"{('/' + r['expected_intent']) if r.get('expected_intent') else ''} | "
                f"{got} |"
            )
    else:
        lines.append("Нет расхождений.")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live-прогон GoldSet на живой модели")
    parser.add_argument("--provider", help="Провайдер (по умолчанию LLM_PROVIDER из .env)")
    parser.add_argument("--model", help="Модель (по умолчанию из настроек провайдера)")
    parser.add_argument(
        "--label",
        default="",
        help="Суффикс файлов отчёта: docs/EVAL_LIVE_<label>.md (пусто — EVAL_LIVE.md)",
    )
    args = parser.parse_args()

    settings = llm_agent.settings_from_env(provider=args.provider)
    if not settings.is_configured:
        print("LLM не настроена: задайте LLM_PROVIDER/LLM_API_KEY/LLM_BASE_URL в .env")
        return 1
    if args.model:
        settings = dataclasses.replace(settings, model=args.model)

    suffix = f"_{args.label}" if args.label else ""
    live_md = DOCS_DIR / f"EVAL_LIVE{suffix}.md"
    live_json = DOCS_DIR / f"EVAL_LIVE{suffix}.json"

    print(f"Live-прогон: provider={settings.provider}, model={settings.model}")
    frame = pd.read_csv(FIXTURE_PATH, parse_dates=["date"])
    profile = {
        "date_min": str(frame["date"].min().date()),
        "date_max": str(frame["date"].max().date()),
    }
    cases = load_cases()
    with tempfile.TemporaryDirectory(prefix="eval-live-audit-") as audit_tmp:
        results, metrics = run_all(
            settings,
            cases=cases,
            frame=frame,
            profile=profile,
            audit_dir=Path(audit_tmp),
        )

    markdown = render_markdown(results, metrics)
    live_md.write_text(markdown, encoding="utf-8")
    live_json.write_text(
        json.dumps({"metrics": metrics, "cases": results}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"\nStatus: {metrics['status_accuracy']}%  DSL: {metrics['dsl_accuracy']}%  "
          f"Fact-check: {metrics['fact_check']['accuracy']}%")
    print(f"Отчёт: {live_md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
