"""Аудит шагов LangGraph: события, отсутствие вопроса и секретов."""

from __future__ import annotations

import json

from onec_analyst.agent.graph import run_agent_graph
from onec_analyst.agent.state import initial_state
from onec_analyst.audit import AuditLog, read_audit
from onec_analyst.llm_agent import LLMError, LLMSettings

from tests.agent_support import (
    FakeLLMClient,
    clarify_response,
    json_response,
    refuse_response,
    text_response,
    tool_response,
)


def _settings() -> LLMSettings:
    return LLMSettings(
        provider="deepseek",
        model="deepseek-chat",
        api_key="SECRET-CREDENTIAL",
        base_url="http://example.invalid/v1",
    )


def _run(audit: AuditLog, responses, *, supports_tools: bool, question: str):
    client = FakeLLMClient(responses, supports_tools=supports_tools)
    state = initial_state(question, client=client, audit=audit)
    final = run_agent_graph(
        state,
        client=client,
        settings=_settings(),
        audit=audit,
    )
    return final, read_audit(audit.path)


def test_ok_path_writes_graph_decision_and_validation(tmp_path) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    final, records = _run(
        audit,
        [tool_response("total", {"start_date": "2024-01-01", "end_date": "2024-12-31"})],
        supports_tools=True,
        question="Сколько продали за 2024?",
    )

    assert final["decision"].status == "ok"
    events = [record["event"] for record in records]
    assert events[0] == "graph_start"
    assert "agent_decision" in events
    assert "validate_ok" in events
    assert events[-1] == "graph_end"
    assert next(r for r in records if r["event"] == "graph_end")["intent"] == "total"


def test_clarify_and_refuse_are_audited(tmp_path) -> None:
    clarify_log = AuditLog(tmp_path / "clarify.jsonl")
    final, records = _run(
        clarify_log,
        [clarify_response(["period2"], "За какой период сравнить?")],
        supports_tools=False,
        question="Сравни продажи",
    )
    assert final["decision"].status == "clarify"
    assert any(record["event"] == "clarify" for record in records)

    refuse_log = AuditLog(tmp_path / "refuse.jsonl")
    final, records = _run(
        refuse_log,
        [refuse_response("Средний чек не поддерживается.")],
        supports_tools=False,
        question="Какой средний чек?",
    )
    assert final["decision"].status == "refuse"
    assert any(record["event"] == "refuse" for record in records)


def test_fix_path_audits_validate_fail_and_retry(tmp_path) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    final, records = _run(
        audit,
        [
            json_response("total", {"start_date": "вчера", "end_date": "2024-12-31"}),
            json_response("total", {"start_date": "2024-01-01", "end_date": "2024-12-31"}),
        ],
        supports_tools=False,
        question="Сколько продали за 2024?",
    )

    assert final["decision"].status == "ok"
    events = [record["event"] for record in records]
    assert "validate_fail" in events
    assert "fix_dsl" in events
    assert events.count("validate_ok") == 1


def test_audit_has_no_question_or_credentials(tmp_path) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    question = "СЕКРЕТНЫЙ ВОПРОС: сколько продали за 2024?"
    _run(
        audit,
        [tool_response("total", {"start_date": "2024-01-01", "end_date": "2024-12-31"})],
        supports_tools=True,
        question=question,
    )

    raw = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert question not in raw
    assert "SECRET-CREDENTIAL" not in raw
    assert "СЕКРЕТНЫЙ" not in raw
    payloads = [json.loads(line) for line in raw.splitlines() if line.strip()]
    assert all("question" not in payload and "raw_response" not in payload for payload in payloads)


def test_broken_audit_does_not_break_answer(tmp_path) -> None:
    class BrokenAudit(AuditLog):
        def record(self, event: str, **fields) -> None:  # type: ignore[override]
            raise OSError("disk full")

    audit = BrokenAudit(tmp_path / "audit.jsonl")
    final, _ = _run(
        audit,
        [tool_response("total", {"start_date": "2024-01-01", "end_date": "2024-12-31"})],
        supports_tools=True,
        question="Сколько продали за 2024?",
    )

    assert final["decision"].status == "ok"


def test_every_event_has_step_and_duration(tmp_path) -> None:
    """Каждое событие пишет step=event; у шагов и graph_end есть duration_ms int ≥ 0."""
    audit = AuditLog(tmp_path / "audit.jsonl")
    _run(
        audit,
        [
            json_response("total", {"start_date": "вчера", "end_date": "2024-12-31"}),
            json_response("total", {"start_date": "2024-01-01", "end_date": "2024-12-31"}),
        ],
        supports_tools=False,
        question="Сколько продали за 2024?",
    )
    records = read_audit(audit.path)

    assert records
    for record in records:
        assert record["step"] == record["event"], record
    # graph_start — маркер начала, без duration_ms; у остальных он есть.
    for record in records:
        if record["event"] == "graph_start":
            assert "duration_ms" not in record
            continue
        assert isinstance(record["duration_ms"], int), record
        assert record["duration_ms"] >= 0, record


def test_failed_llm_call_is_counted_in_audit(tmp_path) -> None:
    """Ошибочный вызов LLM всё равно учитывается в llm_calls (DECISIONS #24)."""
    audit = AuditLog(tmp_path / "audit.jsonl")

    class Broken:
        supports_tools = False

        def complete(self, messages, tools=None):
            raise LLMError("провайдер не ответил за 65 с")

    client = Broken()
    state = initial_state("Сколько продали за 2024?", client=client, audit=audit)
    final = run_agent_graph(state, client=client, settings=_settings(), audit=audit)

    assert final["decision"].status == "error"
    records = read_audit(audit.path)
    graph_end = [record for record in records if record["event"] == "graph_end"][-1]
    assert graph_end["llm_calls"] >= 1, "ошибочный вызов должен быть посчитан"
    decisions = [r for r in records if r["event"] == "agent_decision"]
    assert decisions and decisions[-1]["llm_calls"] >= 1


def test_invalid_response_is_error_without_fallback_event(tmp_path) -> None:
    """Невалидный ответ модели: status=error, события fallback нет (DECISIONS #22)."""
    audit = AuditLog(tmp_path / "audit.jsonl")
    final, records = _run(
        audit,
        [text_response("не знаю")],
        supports_tools=False,
        question="Итог за 2024",
    )

    assert final["decision"].status == "error"
    assert not any(record["event"] == "fallback" for record in records)
    graph_end = [record for record in records if record["event"] == "graph_end"][-1]
    assert graph_end["status"] == "error"
    assert graph_end["fallback"] is False


def test_graph_end_has_status_fallback_and_error_type(tmp_path) -> None:
    """graph_end содержит status и fallback; при ошибке — error_type категории."""
    audit = AuditLog(tmp_path / "audit.jsonl")
    _run(
        audit,
        [text_response("не знаю")],
        supports_tools=False,
        question="Расскажи анекдот",
    )
    records = read_audit(audit.path)
    graph_end = [record for record in records if record["event"] == "graph_end"][-1]
    assert graph_end["status"] == "error"
    assert graph_end["fallback"] is False
    assert graph_end["error_type"] == "unhandled"


def test_error_type_is_category_not_literal(tmp_path) -> None:
    """validate_fail/fix_dsl пишут error_type=validation, а не литерал «error»."""
    audit = AuditLog(tmp_path / "audit.jsonl")
    _run(
        audit,
        [
            json_response("total", {"start_date": "вчера", "end_date": "2024-12-31"}),
            json_response("total", {"start_date": "вчера", "end_date": "2024-12-31"}),
        ],
        supports_tools=False,
        question="Сколько продали за 2024?",
    )
    records = read_audit(audit.path)
    for record in records:
        if record["event"] in ("validate_fail", "fix_dsl"):
            assert record["error_type"] == "validation", record
