"""Запись кассет GoldSet на живой модели (этап 4, не в CI).

Для каждого кейса из ``tests/golden/cases.json`` собирает ``initial_state``,
прогоняет реальный граф на провайдере из окружения и перехватывает сырые
``CompletionResult`` каждого LLM-вызова через :class:`RecordingClient`.
В кассету пишутся только ``responses`` (сырые ответы модели) и
``recorded_outcome`` (снимок status/intent, что граф сделал) — без question,
messages и ключей.

Использование::

    python tools/record_cassettes.py                 # все 50, провайдер из .env
    python tools/record_cassettes.py --case test_001_top_simple
    python tools/record_cassettes.py --force         # перезаписать recorded
    python tools/record_cassettes.py --provider gigachat --model GigaChat-2-Pro

Без настроенного провайдера или доступа к модели скрипт останавливается и не
создаёт фейковых кассет. Существующие recorded-кассеты не перезаписываются без
``--force``.
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

from onec_analyst import llm_agent  # noqa: E402
from onec_analyst.agent.graph import run_agent_graph  # noqa: E402
from onec_analyst.agent.llm import make_agent_client  # noqa: E402
from onec_analyst.agent.state import (  # noqa: E402
    CallBudgetGuard,
    initial_state,
    status_of,
)
from onec_analyst.audit import AuditLog  # noqa: E402
from onec_analyst.llm_agent import CompletionResult, LLMError  # noqa: E402

from tests.golden.cassette_client import completion_to_entry  # noqa: E402
from tests.golden.schema import load_cases  # noqa: E402

CASSETTE_DIR = _ROOT / "tests" / "golden" / "cassettes"


class RecordingClient:
    """Обёртка LLM-клиента: перехватывает сырые ``CompletionResult`` вызовов.

    Сетевые/аутентификационные ошибки (``LLMError``) не попадают в ``responses``
    — они фиксируются в ``errors``, и такой кейс не пишется как кассета.
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self.responses: list[CompletionResult] = []
        self.errors: list[LLMError] = []

    @property
    def supports_tools(self) -> bool:
        return bool(getattr(self._client, "supports_tools", False))

    def complete(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
    ) -> CompletionResult:
        try:
            result = self._client.complete(messages, tools=tools)
        except LLMError as exc:
            self.errors.append(exc)
            raise
        self.responses.append(result)
        return result


def build_state(case: dict[str, Any]) -> dict[str, Any]:
    """``initial_state`` кейса: question, history_dsl, result_entities, mock_today."""
    today = None
    if case.get("mock_today"):
        today = date.fromisoformat(str(case["mock_today"]))
    return initial_state(
        case["question"],
        previous_dsl=case.get("history_dsl"),
        result_entities=case.get("result_entities"),
        today=today,
    )


def load_existing(case_id: str) -> dict[str, Any] | None:
    """Прочитать существующую кассету кейса (или ``None``)."""
    path = CASSETTE_DIR / f"{case_id}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def cassette_path(case_id: str) -> Path:
    return CASSETTE_DIR / f"{case_id}.json"


def record_one(
    case: dict[str, Any],
    settings: Any,
    *,
    force: bool,
    provider: str,
    model: str,
    recorded_at: str,
    audit_dir: Path,
) -> tuple[str, str]:
    """Записать один кейс. Возвращает ``(case_id, status)``.

    ``status``: ``ok`` | ``skipped`` (уже recorded) | ``error`` (сеть/доступ).
    """
    case_id = case["id"]
    existing = load_existing(case_id)
    if existing and existing.get("source") == "recorded" and not force:
        return case_id, "skipped"

    recording = RecordingClient(make_agent_client(settings))
    state = build_state(case)
    final = run_agent_graph(
        state,
        client=recording,
        settings=settings,
        guard=CallBudgetGuard(3),
        audit=AuditLog(audit_dir / f"{case_id}.jsonl"),
    )

    if recording.errors:
        reason = recording.errors[0]
        print(f"  {case_id}: СЕТЬ/ДОСТУП — {type(reason).__name__}: {reason}")
        return case_id, "error"

    decision = final.get("decision")
    outcome = {
        "status": status_of(decision).value,
        "intent": decision.intent if decision is not None else None,
    }
    payload = {
        "case_id": case_id,
        "source": "recorded",
        "recorded_at": recorded_at,
        "provider": provider,
        "model": model,
        "responses": [completion_to_entry(result) for result in recording.responses],
        "recorded_outcome": outcome,
    }
    if not payload["responses"]:
        print(f"  {case_id}: нет ответов модели — кассета не записана")
        return case_id, "error"

    cassette_path(case_id).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return case_id, "ok"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Записать кассеты GoldSet на живой модели")
    parser.add_argument("--provider", help="Провайдер (по умолчанию LLM_PROVIDER из .env)")
    parser.add_argument("--model", help="Модель (по умолчанию из настроек провайдера)")
    parser.add_argument("--case", help="Записать только один кейс (case_id)")
    parser.add_argument("--force", action="store_true", help="Перезаписать recorded-кассеты")
    args = parser.parse_args(argv)

    settings = llm_agent.settings_from_env(provider=args.provider)
    if not settings.is_configured:
        print(
            "LLM не настроена: задайте LLM_PROVIDER/LLM_API_KEY/LLM_BASE_URL в .env. "
            "Кассеты не создаются."
        )
        return 1
    if args.model:
        settings = dataclasses.replace(settings, model=args.model)

    provider = settings.provider
    model = settings.model
    print(f"Запись кассет: provider={provider}, model={model}")
    print(f"Каталог: {CASSETTE_DIR}")

    cases = load_cases()
    if args.case:
        cases = [case for case in cases if case["id"] == args.case]
        if not cases:
            print(f"Кейс {args.case!r} не найден в cases.json")
            return 1

    recorded_at = date.today().isoformat()
    results: dict[str, int] = {}
    with tempfile.TemporaryDirectory(prefix="cassette-audit-") as audit_tmp:
        audit_dir = Path(audit_tmp)
        for case in cases:
            case_id, status = record_one(
                case,
                settings,
                force=args.force,
                provider=provider,
                model=model,
                recorded_at=recorded_at,
                audit_dir=audit_dir,
            )
            results[status] = results.get(status, 0) + 1
            print(f"  {case_id}: {status}")

    print("\nИтог:", ", ".join(f"{status}={count}" for status, count in sorted(results.items())))
    if results.get("error"):
        print("Есть ошибки доступа/сети: фейковые кассеты не создавались.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
