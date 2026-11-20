"""Лимиты LLM по ТЗ 6.4 §4.2: атомарный резерв, guard на каждый вызов, интеграция с графом.

Проверяется переход от «учёта запросов» к «учёту вызовов»: каждый успешный вызов
(``resolve_context``/``agent_decision``/``fix_dsl``) резервирует слот атомарно;
сетевой сбой возвращает резерв; при исчерпании лимита граф уходит в fallback без
обращения к API.
"""

from __future__ import annotations

from datetime import date

import pytest
from streamlit.testing.v1 import AppTest

from onec_analyst import llm_agent
from onec_analyst.agent.graph import run_agent_graph
from onec_analyst.agent.state import (
    CallBudgetGuard,
    CompositeGuard,
    initial_state,
)
from onec_analyst.llm_agent import CompletionResult, LLMError
from onec_analyst.rate_limiter import (
    MAX_GLOBAL,
    MAX_PER_SESSION,
    RateLimiter,
    RateLimiterGuard,
)

from tests.agent_support import FakeLLMClient, json_response
from tests.paths import APP_FILE

TODAY = date(2026, 11, 18)
TIMEOUT = 120


def _guard(limiter: RateLimiter, session_id: str) -> CompositeGuard:
    """Композитный guard как в app.py: потолок 3 + лимит сессии/дня."""
    return CompositeGuard(
        CallBudgetGuard(3),
        RateLimiterGuard(limiter, session_id),
    )


# --------------------------------------------------------------------------- #
# try_consume / release: атомарность
# --------------------------------------------------------------------------- #
def test_try_consume_reserves_and_counts(rate_limit_db_path) -> None:
    """try_consume резервирует слот и отражается в check_limit."""
    limiter = RateLimiter(rate_limit_db_path)
    ok, status = limiter.try_consume("s1")

    assert ok is True
    assert status.used_session == 1
    assert status.used_global == 1
    assert limiter.check_limit("s1").used_session == 1


def test_try_consume_blocks_at_session_limit(rate_limit_db_path) -> None:
    """16-й резерв отклоняется с причиной session и без инкремента."""
    limiter = RateLimiter(rate_limit_db_path)
    for _ in range(MAX_PER_SESSION):
        assert limiter.try_consume("s1")[0] is True

    ok, status = limiter.try_consume("s1")
    assert ok is False
    assert status.reason == "session"
    assert status.used_session == MAX_PER_SESSION


def test_try_consume_blocks_at_global_limit(rate_limit_db_path) -> None:
    """Глобальные 100 вызовов блокируют новые сессии."""
    limiter = RateLimiter(rate_limit_db_path)
    for index in range(MAX_GLOBAL):
        limiter.increment(f"other-{index}")

    ok, status = limiter.try_consume("s1")
    assert ok is False
    assert status.reason == "global"
    assert status.used_global == MAX_GLOBAL


def test_release_decrements_not_below_zero(rate_limit_db_path) -> None:
    """release снимает один резерв; лишние вызовы не уходят в минус."""
    limiter = RateLimiter(rate_limit_db_path)
    limiter.try_consume("s1")

    limiter.release("s1")
    assert limiter.check_limit("s1").used_session == 0

    limiter.release("s1")  # повторный release — не ниже нуля
    assert limiter.check_limit("s1").used_session == 0


def test_guard_after_failure_returns_reservation(rate_limit_db_path) -> None:
    """before_call → after_failure оставляет счётчик нулевым (сбой не съедает)."""
    limiter = RateLimiter(rate_limit_db_path)
    guard = _guard(limiter, "s1")

    assert guard.before_call() is True
    guard.after_failure()

    assert limiter.check_limit("s1").used_session == 0


def test_guard_after_success_keeps_reservation(rate_limit_db_path) -> None:
    """before_call → after_success фиксирует вызов."""
    limiter = RateLimiter(rate_limit_db_path)
    guard = _guard(limiter, "s1")

    assert guard.before_call() is True
    guard.after_success()

    assert limiter.check_limit("s1").used_session == 1


# --------------------------------------------------------------------------- #
# Интеграция с графом: учёт каждого вызова
# --------------------------------------------------------------------------- #
def test_three_llm_calls_increment_by_three(rate_limit_db_path) -> None:
    """Запрос из трёх вызовов (resolve_context + agent_decision + fix_dsl) = +3."""
    limiter = RateLimiter(rate_limit_db_path)
    guard = _guard(limiter, "s1")
    client = FakeLLMClient(
        [
            CompletionResult(
                content='{"resolved_question": "Сколько продали за 2024 год?"}'
            ),
            json_response("total", {"start_date": 2024}),  # невалидно → fix_dsl
            CompletionResult(
                content='{"start_date": "2024-01-01", "end_date": "2024-12-31"}'
            ),
        ]
    )
    state = initial_state(
        "А что было тогда же?",
        previous_dsl={"intent": "total", "params": {}},
        today=TODAY,
        client=client,
        guard=guard,
    )

    final = run_agent_graph(state, client=client, guard=guard)

    assert client.calls == 3
    assert final["decision"].status == "ok"
    status = limiter.check_limit("s1")
    assert status.used_session == 3
    assert status.used_global == 3


def test_sixteenth_call_falls_back_without_api(rate_limit_db_path) -> None:
    """После 15 вызовов следующий запрос уходит в fallback, API не вызывается."""
    limiter = RateLimiter(rate_limit_db_path)
    for _ in range(MAX_PER_SESSION):
        limiter.increment("s1")

    guard = _guard(limiter, "s1")
    client = FakeLLMClient(
        [json_response("total", {"start_date": "2024-01-01", "end_date": "2024-12-31"})]
    )
    state = initial_state(
        "Сколько мы продали за 2024 год?",
        today=TODAY,
        client=client,
        guard=guard,
    )

    final = run_agent_graph(state, client=client, guard=guard)

    assert client.calls == 0
    assert final["decision"].status == "fallback"
    assert "handle_error" in final["trace"]
    assert limiter.check_limit("s1").used_session == MAX_PER_SESSION


def test_network_failure_does_not_consume(rate_limit_db_path) -> None:
    """Сетевой сбой: резерв возвращается, счётчик не растёт."""

    class FailingClient:
        supports_tools = False

        def complete(self, messages, tools=None):
            raise LLMError("сеть недоступна")

    limiter = RateLimiter(rate_limit_db_path)
    guard = _guard(limiter, "s1")
    client = FailingClient()
    state = initial_state(
        "Сколько мы продали за 2024 год?",
        today=TODAY,
        client=client,
        guard=guard,
    )

    final = run_agent_graph(state, client=client, guard=guard)

    assert limiter.check_limit("s1").used_session == 0
    assert final["decision"].status == "fallback"


def test_composite_budget_caps_calls_per_request(rate_limit_db_path) -> None:
    """Потолок CallBudgetGuard(3) не даёт запросу больше трёх вызовов."""
    limiter = RateLimiter(rate_limit_db_path)
    guard = _guard(limiter, "s1")
    client = FakeLLMClient(
        [json_response("total", {"start_date": "2024-01-01", "end_date": "2024-12-31"})]
    )
    state = initial_state(
        "Сколько мы продали за 2024 год?",
        today=TODAY,
        client=client,
        guard=guard,
    )

    run_agent_graph(state, client=client, guard=guard)

    assert client.calls == 1  # обычный успешный запрос — один вызов


# --------------------------------------------------------------------------- #
# AppTest: счётчик UI отражает вызовы, а не запросы
# --------------------------------------------------------------------------- #
def test_app_graph_path_counts_calls(
    monkeypatch: pytest.MonkeyPatch, rate_limit_db_path
) -> None:
    """Граф-путь с одним вызовом показывает «Внешняя квота ИИ: 1/15»."""
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("LLM_MODEL", "deepseek-chat")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    client = FakeLLMClient(
        [json_response("total", {"start_date": "2024-01-01", "end_date": "2024-12-31"})]
    )
    monkeypatch.setattr(llm_agent, "make_client", lambda settings: client)

    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()
    at.text_input[0].set_value("Сколько мы продали за 2024 год?")
    next(button for button in at.button if button.label == "Спросить").click()
    at.run()

    captions = [caption.value for caption in at.sidebar.caption]
    assert any("Внешняя квота ИИ: 1/15" in text for text in captions)
    assert len(at.exception) == 0
