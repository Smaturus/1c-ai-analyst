"""LLM-as-judge по GoldSet: бинарная рубрика качества ответа (не в CI).

Судья оценивает сгенерированный ответ (сводку/отказ/уточнение) отдельной
моделью, по бинарным вопросам. Это сигнал при изменении промптов, а не
абсолютная истина и не CI-гейт (ADR 0013 D5).

Ответ строится из кассеты (replay графа без сети), поэтому прогон судьи тратит
вызовы только на саму модель-судью. Судья — отдельный провайдер
(``JUDGE_PROVIDER``/``JUDGE_MODEL`` или ``--judge-provider/--judge-model``), не
через ``on_llm_call``.

Рубрика (бинарно, «да»/«нет»):
- ok: все числа в тексте совпадают с переданными значениями расчёта (payload);
  выводы следуют из этих значений.
- refuse: отказ объяснён.
- clarify: уточнение относится к missing.

Отчёт: ``docs/EVAL_JUDGE.md`` — доля «да». Не CI-гейт.

Использование::

    python tools/eval_judge.py                 # судья GigaChat-2-Pro
    python tools/eval_judge.py --judge-provider deepseek --judge-model deepseek-chat
"""

from __future__ import annotations

import argparse
import json
import re
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
from onec_analyst.agent.state import CallBudgetGuard, initial_state, status_of  # noqa: E402
from onec_analyst.audit import AuditLog  # noqa: E402
from onec_analyst.pipeline import execute  # noqa: E402
from tests.golden.cassette_client import CassetteClient  # noqa: E402
from tests.golden.schema import FIXTURE_PATH, load_cases  # noqa: E402

CASSETTE_DIR = _ROOT / "tests" / "golden" / "cassettes"
DOCS_DIR = _ROOT / "docs"
JUDGE_MD = DOCS_DIR / "EVAL_JUDGE.md"
JUDGE_JSON = DOCS_DIR / "EVAL_JUDGE.json"

#: 15 кейсов рубрики (GoldenSet.py отсутствует в репозитории — список зафиксирован
#: здесь). 10 ok / 2 clarify / 3 refuse.
JUDGE_SAMPLE: tuple[str, ...] = (
    "test_001_top_simple",
    "test_003_total",
    "test_004_timeseries",
    "test_005_negative",
    "test_007_counterparty",
    "test_010_clarify_missing_dates",
    "test_012_unknown_greeting",
    "test_014_filter_product",
    "test_015_aggregated_metric",
    "test_025_total_year",
    "test_026_edge_future_date",
    "test_028_group_specific",
    "test_032_quantity",
    "test_042_dangling_pronoun_no_history",
    "test_049_total_quarter",
)

_SYSTEM = (
    "Ты — строгий ревизор ответов аналитического ассистента по продажам. "
    "Отвечай РОВНО ОДНИМ словом: «да» или «нет». Без пояснений."
)


def _ask(client: Any, question: str) -> str:
    result = client.chat([{"role": "system", "content": _SYSTEM}, {"role": "user", "content": question}])
    return (result or "").strip()


def _parse_yes_no(text: str) -> bool | None:
    """Первое слово «да»/«нет» (или yes/no)."""
    lowered = text.lower()
    if re.match(r"^(да|yes)\b", lowered):
        return True
    if re.match(r"^(нет|no)\b", lowered):
        return False
    if "да" in lowered and "нет" not in lowered:
        return True
    if "нет" in lowered and "да" not in lowered:
        return False
    return None


def build_answer(case: dict[str, Any], frame: pd.DataFrame, profile: dict[str, str], audit_dir: Path) -> dict[str, Any]:
    """Воспроизвести ответ системы из кассеты: сводка (ok), отказ или уточнение."""
    case_id = case["id"]
    client = CassetteClient.load(CASSETTE_DIR / f"{case_id}.json")
    today = None
    if case.get("mock_today"):
        today = date.fromisoformat(str(case["mock_today"]))
    final = run_agent_graph(
        initial_state(
            case["question"],
            previous_dsl=case.get("history_dsl"),
            result_entities=case.get("result_entities"),
            today=today,
        ),
        client=client,
        guard=CallBudgetGuard(3),
        audit=AuditLog(audit_dir / f"{case_id}.jsonl"),
    )
    decision = final.get("decision")
    status = status_of(decision).value
    answer: dict[str, Any] = {
        "case_id": case_id,
        "question": case["question"],
        "status": status,
        "dsl": decision.dsl if decision is not None else None,
    }
    if status == "ok":
        execution = execute(decision, frame, profile)
        answer["summary"] = execution.summary
        answer["payload"] = [dict(value) for value in execution.summary_payload.values]
    elif status == "refuse":
        answer["summary"] = decision.refusal_reason if decision is not None else ""
    elif status == "clarify":
        clarification = decision.clarification if decision is not None else None
        answer["summary"] = clarification.question if clarification is not None else ""
        answer["missing"] = list(clarification.missing) if clarification is not None else []
    else:
        answer["summary"] = (decision.error if decision is not None else "") or ""
    return answer


def judge_answer(client: Any, answer: dict[str, Any]) -> dict[str, Any]:
    """Задать бинарные вопросы судье по статусу ответа."""
    status = answer["status"]
    question = answer["question"]
    summary = answer["summary"]
    dsl = json.dumps(answer["dsl"], ensure_ascii=False) if answer.get("dsl") else "—"
    payload = (
        json.dumps(answer["payload"], ensure_ascii=False)
        if answer.get("payload")
        else "—"
    )

    base = (
        f"Вопрос пользователя: {question}\n"
        f"Значения расчёта (payload): {payload}\n"
        f"Сгенерированный ответ: {summary}\n"
        f"DSL: {dsl}\n\n"
    )

    verdicts: dict[str, bool | None] = {}
    if status == "ok":
        verdicts["numbers_match_payload"] = _parse_yes_no(
            _ask(
                client,
                base
                + "Все ли числа в тексте совпадают с переданными значениями расчёта? (да/нет)",
            )
        )
        verdicts["conclusions_follow"] = _parse_yes_no(
            _ask(
                client,
                base
                + "Следуют ли выводы в ответе из переданных значений расчёта? (да/нет)",
            )
        )
    elif status == "refuse":
        verdicts["refusal_explained"] = _parse_yes_no(
            _ask(client, base + "Объяснён ли отказ понятно и по существу вопроса? (да/нет)")
        )
    elif status == "clarify":
        missing = json.dumps(answer.get("missing", []), ensure_ascii=False)
        verdicts["clarification_matches_missing"] = _parse_yes_no(
            _ask(
                client,
                base + f"Относится ли уточняющий вопрос к недостающим данным (missing={missing})? (да/нет)",
            )
        )
    return {"case_id": answer["case_id"], "status": status, "verdicts": verdicts}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LLM-as-judge по GoldSet")
    parser.add_argument("--judge-provider", help="Провайдер судьи (JUDGE_PROVIDER или gigachat)")
    parser.add_argument("--judge-model", help="Модель судьи (JUDGE_MODEL или GigaChat-2-Pro)")
    args = parser.parse_args(argv)

    judge_env = llm_agent._environment_with_dotenv()
    judge_provider = args.judge_provider or judge_env.get("JUDGE_PROVIDER") or "gigachat"
    judge_model = args.judge_model or judge_env.get("JUDGE_MODEL")

    settings = llm_agent.settings_from_env(provider=judge_provider)
    if not settings.is_configured:
        print(f"Судья не настроен: provider={judge_provider}. Задайте ключ в .env")
        return 1
    if judge_model:
        import dataclasses

        settings = dataclasses.replace(settings, model=judge_model)

    print(f"Судья: provider={settings.provider}, model={settings.model}")

    frame = pd.read_csv(FIXTURE_PATH, parse_dates=["date"])
    profile = {
        "date_min": str(frame["date"].min().date()),
        "date_max": str(frame["date"].max().date()),
    }
    cases = {c["id"]: c for c in load_cases()}
    sample = [cases[cid] for cid in JUDGE_SAMPLE if cid in cases]

    judge_client = llm_agent.make_client(settings)
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="eval-judge-audit-") as audit_tmp:
        audit_dir = Path(audit_tmp)
        for case in sample:
            answer = build_answer(case, frame, profile, audit_dir)
            verdict = judge_answer(judge_client, answer)
            verdict["question"] = case["question"]
            results.append(verdict)
            print(f"  {verdict['case_id']}: {verdict['verdicts']}")

    yes = 0
    total = 0
    groups: dict[str, list[int]] = {
        "refuse/clarify": [0, 0],
        "ok_numbers": [0, 0],
        "ok_conclusions": [0, 0],
    }
    for r in results:
        for key, value in r["verdicts"].items():
            group = (
                "ok_numbers"
                if key == "numbers_match_payload"
                else "ok_conclusions"
                if key == "conclusions_follow"
                else "refuse/clarify"
            )
            groups[group][1] += 1
            total += 1
            if value is True:
                yes += 1
                groups[group][0] += 1
    share = round(100.0 * yes / total, 1) if total else 0.0
    group_stats = {
        name: {
            "yes": hit,
            "total": count,
            "share": round(100.0 * hit / count, 1) if count else 0.0,
        }
        for name, (hit, count) in groups.items()
    }

    payload = {
        "provider": settings.provider,
        "model": settings.model,
        "date": date.today().isoformat(),
        "yes": yes,
        "total": total,
        "share_yes": share,
        "groups": group_stats,
        "cases": results,
    }
    JUDGE_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# EVAL Judge",
        "",
        f"- Судья: **{settings.provider} / {settings.model}**",
        f"- Дата: {date.today().isoformat()}",
        f"- Доля «да»: **{share}%** ({yes}/{total})",
        "",
        "## Сводка по группам",
        "",
        "| Группа | «да» |",
        "|---|---|",
    ]
    for name, label in (
        ("refuse/clarify", "refuse/clarify — объяснение/уточнение"),
        ("ok_numbers", "ok — числа (payload)"),
        ("ok_conclusions", "ok — выводы"),
    ):
        stats = group_stats[name]
        lines.append(f"| {label} | {stats['yes']}/{stats['total']} ({stats['share']}%) |")
    lines.extend(
        [
            "",
            "## Результаты по кейсам",
            "",
            "| Кейс | Статус | Вердикты |",
            "|---|---|---|",
        ]
    )
    for r in results:
        verdicts = ", ".join(f"{k}={('да' if v is True else 'нет' if v is False else '?')}" for k, v in r["verdicts"].items())
        lines.append(f"| {r['case_id']} | {r['status']} | {verdicts} |")
    lines.append("")
    JUDGE_MD.write_text("\n".join(lines), encoding="utf-8")

    print(f"\nДоля «да»: {share}% ({yes}/{total})")
    print(f"Отчёт: {JUDGE_MD}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
