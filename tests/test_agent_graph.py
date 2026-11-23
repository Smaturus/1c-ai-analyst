"""Маршрутизация графа, аварийные ответы модели, AppTest LLM-пути."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from onec_analyst import llm_agent
from onec_analyst.agent import graph as agent_graph
from onec_analyst.agent.graph import (
    DEFAULT_RECURSION_LIMIT,
    route_decision,
    route_entry,
    route_validation,
    run_agent_graph,
)
from onec_analyst.agent.state import CallBudgetGuard, initial_state
from onec_analyst.dispatcher import process_request
from onec_analyst.llm_agent import AgentDecision, LLMError, LLMSettings, CompletionResult
from onec_analyst.rate_limiter import RateLimiter
from onec_analyst.session import ConversationState

from tests.agent_support import (
    FakeLLMClient,
    clarify_response,
    json_response,
    refuse_response,
    text_response,
    tool_response,
    two_tool_response,
)
from tests.paths import APP_FILE

TODAY = date(2026, 11, 18)
PROFILE = {"date_min": "2023-01-15", "date_max": "2024-03-05"}
TIMEOUT = 120


def _settings() -> LLMSettings:
    """Настройки с фиктивным ключом: сеть не нужна."""
    return LLMSettings(provider="deepseek", model="deepseek-chat", api_key="test-key")


def _run(question: str, responses, **kwargs):
    """Запустить граф с fake-клиентом и фиксированной датой."""
    client = FakeLLMClient(responses)
    state = initial_state(question, today=TODAY, client=client, **kwargs)
    return run_agent_graph(state, client=client), client


# --------------------------------------------------------------------------- #
# Маршрутизация
# --------------------------------------------------------------------------- #
def test_entry_skips_context_without_memory() -> None:
    """Нет previous_dsl — контекст не нужен."""
    state = initial_state("А что с ними?", today=TODAY)
    assert route_entry(state) == "agent_decision"


def test_entry_uses_context_with_memory_and_trigger() -> None:
    """Есть память и триггер — идём через resolve_context."""
    state = initial_state(
        "А теперь покажи топ-3",
        previous_dsl={"intent": "total", "params": {}},
        today=TODAY,
    )
    assert route_entry(state) == "resolve_context"


def test_entry_skips_context_with_memory_but_no_trigger() -> None:
    """Память есть, но вопрос самостоятельный — контекст пропускается."""
    state = initial_state(
        "Сколько мы продали за 2024 год?",
        previous_dsl={"intent": "total", "params": {}},
        today=TODAY,
    )
    assert route_entry(state) == "agent_decision"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("ok", "validate_dsl"),
        ("clarify", "clarify"),
        ("refuse", "refuse"),
        ("error", "handle_error"),
        ("fallback", "handle_error"),
    ],
)
def test_decision_routes_by_status(status: str, expected: str) -> None:
    """Каждый статус ведёт в свою ветку."""
    decision = AgentDecision(ok=status == "ok", status=status)
    assert route_decision({"decision": decision}) == expected


def test_missing_decision_routes_to_handle_error() -> None:
    """Нет решения — сразу обработка ошибки."""
    assert route_decision({"decision": None}) == "handle_error"


def test_validation_routes() -> None:
    """Валидно — конец; ошибка — одна починка, затем handle_error."""
    assert route_validation({}) == agent_graph.END
    assert route_validation({"validation_error": "bad", "retry_count": 0}) == "fix_dsl"
    assert route_validation({"validation_error": "bad", "retry_count": 1}) == "handle_error"


# --------------------------------------------------------------------------- #
# Успешные и терминальные статусы
# --------------------------------------------------------------------------- #
def test_ok_decision_is_validated() -> None:
    """Один tool call → валидация → END, без расчёта внутри графа."""
    final, client = _run(
        "Сколько мы продали за 2024 год?",
        [tool_response("total", {"start_date": "2024-01-01", "end_date": "2024-12-31"})],
    )

    assert final["decision"].status == "ok"
    assert final["trace"] == ["agent_decision", "validate_dsl"]
    assert final["llm_success"] is True
    assert client.calls == 1


def test_clarify_stops_before_validation() -> None:
    """Уточнение не валидируется и не исполняется."""
    final, _ = _run("Сравни топы", [clarify_response(["period2"], "За какой период?")])

    assert final["decision"].status == "clarify"
    assert final["decision"].clarification.missing == ["period2"]
    assert final["trace"] == ["agent_decision", "clarify"]


def test_refuse_stops_before_validation() -> None:
    """Отказ не валидируется и не исполняется, причина сохранена."""
    final, _ = _run("Средний чек", [refuse_response("Средний чек не поддерживается.")])

    assert final["decision"].status == "refuse"
    assert "не поддерживается" in final["decision"].refusal_reason
    assert final["trace"] == ["agent_decision", "refuse"]


# --------------------------------------------------------------------------- #
# Аварийные ответы модели → handle_error
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("response", "marker"),
    [
        (CompletionResult(), "инструмент"),
        (text_response("Похоже, это про продажи"), "инструмент"),
        (two_tool_response(), "инструментов"),
    ],
)
def test_bad_model_answers_go_to_handle_error(response, marker: str) -> None:
    """0/2 вызова и свободный текст не угадываются, а уходят в handle_error."""
    final, _ = _run("Сколько мы продали за 2024 год?", [response])

    assert "handle_error" in final["trace"]
    assert final["decision"].status in ("fallback", "error")
    assert marker in (final.get("validation_error") or final["decision"].error or "")


def test_free_text_is_error_with_model_answer_not_rules() -> None:
    """Невалидный ответ модели — ошибка с её текстом, а не подмена правилами."""
    final, _ = _run("Итог за 2024", [text_response("не знаю")])

    assert final["decision"].status == "error"
    assert final["decision"].raw_response == "не знаю"
    assert "Итог за 2024" in final["decision"].llm_request


def test_unparseable_question_is_error_not_guess() -> None:
    """Если и правила не разобрали вопрос — честная ошибка."""
    final, _ = _run("Расскажи анекдот", [text_response("не знаю")])

    assert final["decision"].status == "error"
    assert final["decision"].ok is False


# --------------------------------------------------------------------------- #
# fix_dsl: максимум один раз
# --------------------------------------------------------------------------- #
def test_invalid_then_fix_then_ok() -> None:
    """Ошибка Pydantic → одна починка → валидный DSL."""
    final, client = _run(
        "Сколько мы продали за 2024 год?",
        [
            json_response("total", {"start_date": 2024}),
            CompletionResult(
                content='{"start_date": "2024-01-01", "end_date": "2024-12-31"}'
            ),
        ],
    )

    assert final["decision"].status == "ok"
    assert final["trace"] == [
        "agent_decision",
        "validate_dsl",
        "fix_dsl",
        "validate_dsl",
    ]
    assert client.calls == 2


def test_invalid_twice_no_third_attempt() -> None:
    """Вторая ошибка → handle_error; fix_dsl вызван ровно один раз."""
    final, client = _run(
        "Расскажи анекдот",
        [
            json_response("total", {"unknown": 1}),
            text_response("снова не JSON"),
        ],
    )

    assert final["trace"].count("fix_dsl") == 1
    assert final["retry_count"] == 1
    assert final["decision"].status == "error"
    assert client.calls == 2


# --------------------------------------------------------------------------- #
# Лимит и сбой графа
# --------------------------------------------------------------------------- #
def test_exhausted_guard_blocks_without_rules_and_without_calls() -> None:
    """Лимит исчерпан до вызова — ни обращения к провайдеру, ни ответа правилами."""
    final, client = _run(
        "Сколько мы продали за 2024 год?",
        [json_response("total", {})],
        guard=CallBudgetGuard(limit=0),
    )

    assert client.calls == 0
    # DECISIONS #21: при включённой LLM лимит блокирует, а не подменяется.
    assert final["decision"].status == "error"
    assert "Лимит" in (final["decision"].error or "")
    assert "handle_error" in final["trace"]


def test_graph_crash_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    """Падение графа возвращает status=error, а не исключение в UI."""

    class Broken:
        def invoke(self, *args, **kwargs):
            raise RuntimeError("сломалось")

    monkeypatch.setattr(agent_graph, "compiled_graph", lambda: Broken())
    client = FakeLLMClient([json_response("total", {})])
    final = run_agent_graph(
        initial_state("Итог за 2024", today=TODAY, client=client), client=client
    )

    assert final["decision"].status == "error"
    assert "graph_error" in final["trace"]


def test_on_llm_call_counts_successful_calls() -> None:
    """Колбэк вызывается после каждого успешного ответа провайдера."""
    calls: list[int] = []
    client = FakeLLMClient([json_response("total", {})])
    state = initial_state("Итог за 2024", today=TODAY, client=client)

    run_agent_graph(state, client=client, on_llm_call=lambda: calls.append(1))

    assert calls == [1]


def test_recursion_limit_is_exposed() -> None:
    """Предел шагов задан явно и положителен."""
    assert DEFAULT_RECURSION_LIMIT >= 10


# --------------------------------------------------------------------------- #
# Диспетчер: граф вызывается ровно там, где нужно
# --------------------------------------------------------------------------- #
def test_use_llm_false_never_calls_graph(
    monkeypatch: pytest.MonkeyPatch, sales_frame: pd.DataFrame
) -> None:
    """Офлайн-путь не вызывает граф вообще."""

    def _fail(*args, **kwargs):
        raise AssertionError("граф не должен вызываться при use_llm=False")

    monkeypatch.setattr(agent_graph, "run_agent_graph", _fail)
    outcome = process_request(
        "Итог за 2024", False, ConversationState(), sales_frame, PROFILE
    )

    assert outcome.ok is True
    assert outcome.status == "ok"
    assert outcome.trace == ("plan", "guard", "dispatch", "summary")


def test_use_llm_true_runs_graph_exactly_once(
    monkeypatch: pytest.MonkeyPatch, sales_frame: pd.DataFrame
) -> None:
    """Агентный путь — ровно один запуск графа на запрос."""
    calls = {"n": 0}
    original = agent_graph.run_agent_graph

    def _counted(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(agent_graph, "run_agent_graph", _counted)
    monkeypatch.setattr(
        llm_agent,
        "make_client",
        lambda settings: FakeLLMClient(
            [json_response("total", {"start_date": "2024-01-01", "end_date": "2024-12-31"})]
        ),
    )
    outcome = process_request(
        "Сколько мы продали за 2024 год?",
        True,
        ConversationState(),
        sales_frame,
        PROFILE,
        settings=_settings(),
        today=TODAY,
    )

    assert calls["n"] == 1
    assert outcome.status == "ok"
    assert outcome.intent == "total"
    assert outcome.result is not None
    assert outcome.trace[:2] == ("agent_decision", "validate_dsl")


def test_dispatcher_refuse_has_no_dataframe(
    monkeypatch: pytest.MonkeyPatch, sales_frame: pd.DataFrame
) -> None:
    """Отказ возвращается без DataFrame и сводки."""
    monkeypatch.setattr(
        llm_agent,
        "make_client",
        lambda settings: FakeLLMClient([refuse_response("Нет данных за будущее")]),
    )
    outcome = process_request(
        "Покажи будущее",
        True,
        ConversationState(),
        sales_frame,
        PROFILE,
        settings=_settings(),
        today=TODAY,
    )

    assert outcome.status == "refuse"
    assert outcome.result is None
    assert outcome.summary == ""
    assert outcome.refusal_reason == "Нет данных за будущее"


def test_offline_error_has_explicit_status(
    sales_frame: pd.DataFrame,
) -> None:
    """Ошибка офлайн-пути тоже получает явный status=error."""
    outcome = process_request("", False, ConversationState(), sales_frame, PROFILE)

    assert outcome.ok is False
    assert outcome.status == "error"


# --------------------------------------------------------------------------- #
# AppTest: LLM-путь с fake-клиентом
# --------------------------------------------------------------------------- #
@pytest.fixture()
def llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Окружение с настроенной LLM: ключ фиктивный, сеть не вызывается."""
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("LLM_MODEL", "deepseek-chat")
    monkeypatch.setenv("LLM_API_KEY", "test-key")


def _install_client(monkeypatch: pytest.MonkeyPatch, responses) -> FakeLLMClient:
    """Подменить создание клиента на fake без сети."""
    client = FakeLLMClient(responses)
    monkeypatch.setattr(llm_agent, "make_client", lambda settings: client)
    return client


def _ask(at: AppTest, question: str, monkeypatch: pytest.MonkeyPatch, responses) -> AppTest:
    """Запустить приложение, задать вопрос и дождаться перерисовки."""
    _install_client(monkeypatch, responses)
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()
    at.text_input[0].set_value(question)
    next(button for button in at.button if button.label == "Спросить").click()
    return at.run()


def test_app_llm_path_renders_result(
    llm_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LLM-путь через граф рисует результат и статус в «Технических деталях»."""
    at = _ask(
        AppTest.from_file(APP_FILE, default_timeout=TIMEOUT),
        "Сколько мы продали за 2024 год?",
        monkeypatch,
        [json_response("total", {"start_date": "2024-01-01", "end_date": "2024-12-31"})],
    )

    assert len(at.exception) == 0
    modes = [markdown.value for markdown in at.markdown if "Статус" in markdown.value]
    assert modes, "статус должен быть в «Технических деталях»"
    assert "`ok`" in modes[0]
    # Результат подписан вопросом: иначе старая таблица читается как ответ на новый.
    captions = " ".join(caption.value for caption in at.caption)
    assert "Расчёт по вопросу" in captions, captions
    assert "Сколько мы продали за 2024 год?" in captions, captions


def test_app_long_run_notice_not_left_after_answer(
    llm_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Плашка «не обновляйте страницу» не остаётся после ответа.

    Она рисуется только на время вызова (``st.empty`` + очистка в ``finally``):
    если бы оставалась, пользователь видел бы устаревшее предупреждение под
    каждым ответом.
    """
    at = _ask(
        AppTest.from_file(APP_FILE, default_timeout=TIMEOUT),
        "Сколько мы продали за 2024 год?",
        monkeypatch,
        [json_response("total", {"start_date": "2024-01-01", "end_date": "2024-12-31"})],
    )

    assert len(at.exception) == 0
    captions = [caption.value for caption in at.caption]
    assert not any("Не обновляйте страницу" in text for text in captions), captions


def test_app_cloud_call_consumes_quota(
    llm_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Облачный провайдер по-прежнему расходует квоту (инвариант не отключён)."""
    at = _ask(
        AppTest.from_file(APP_FILE, default_timeout=TIMEOUT),
        "Сколько мы продали за 2024 год?",
        monkeypatch,
        [json_response("total", {"start_date": "2024-01-01", "end_date": "2024-12-31"})],
    )

    assert len(at.exception) == 0
    session_id = str(at.session_state["llm_session_id"])
    assert RateLimiter().check_limit(session_id).used_session >= 1


def test_app_ollama_call_does_not_consume_quota(monkeypatch: pytest.MonkeyPatch) -> None:
    """Локальная модель не тратит внешние токены — квота остаётся нетронутой."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_ENABLED", "true")
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    at = _ask(
        AppTest.from_file(APP_FILE, default_timeout=TIMEOUT),
        "Сколько мы продали за 2024 год?",
        monkeypatch,
        [json_response("total", {"start_date": "2024-01-01", "end_date": "2024-12-31"})],
    )

    assert len(at.exception) == 0
    session_id = str(at.session_state["llm_session_id"])
    status = RateLimiter().check_limit(session_id)
    assert status.used_session == 0, "локальный вызов не должен тратить внешнюю квоту"
    assert status.used_global == 0
    # Вызов действительно состоялся: есть «Технические детали» со статусом.
    assert any("Статус" in markdown.value for markdown in at.markdown)


def test_app_llm_unavailable_shows_error_without_rules(
    llm_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Таймаут LLM при включённой галочке → ошибка с подсказкой, без правил."""

    class FailingClient:
        supports_tools = False

        def complete(self, messages: object, tools: object = None) -> object:
            raise LLMError("провайдер не ответил за 1200 с")

    monkeypatch.setattr(llm_agent, "make_client", lambda settings: FailingClient())
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()
    at.text_input[0].set_value("Сколько мы продали за 2024 год?")
    next(button for button in at.button if button.label == "Спросить").click()
    at.run()

    assert len(at.exception) == 0
    errors = " ".join(item.value for item in at.error)
    assert "LLM `deepseek-chat` не дала ответа" in errors, errors
    assert "детерминированным режимом" in errors, errors
    captions = " ".join(caption.value for caption in at.caption)
    assert "1200" in captions, captions
    # Ответа по правилам нет: ни расчётной подписи режима, ни JSON-DSL.
    assert not any("офлайн-правила" in item.value for item in at.markdown)
    # В «Технических деталях» — ровно тот текст, что ушёл модели.
    marks = " ".join(item.value for item in at.markdown)
    assert "Запрос к модели" in marks, marks
    technical = " ".join(item.value for item in at.code)
    assert "не ответил за 1200" in technical, technical
    assert "===== system =====" in technical, "system-промпт должен быть показан"
    assert "Сколько мы продали за 2024 год?" in technical, "вопрос должен быть показан"


def test_app_invalid_llm_answer_shows_error_with_model_text(
    llm_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Невалидный ответ модели → ошибка с её текстом, без офлайн-правил."""
    monkeypatch.setattr(
        llm_agent,
        "make_client",
        lambda settings: FakeLLMClient([text_response("не знаю")]),
    )
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()
    at.text_input[0].set_value("Итог за 2024")
    next(button for button in at.button if button.label == "Спросить").click()
    at.run()

    assert len(at.exception) == 0
    errors = " ".join(item.value for item in at.error)
    assert "не соответствует контракту" in errors, errors
    assert not any("офлайн-правила" in item.value for item in at.markdown)
    marks = " ".join(item.value for item in at.markdown)
    assert "Ответ модели" in marks, marks
    technical = " ".join(item.value for item in at.code)
    assert "не знаю" in technical, technical


def test_app_refuse_plate(
    llm_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Отказ показывается отдельной плашкой с причиной, без таблицы."""
    at = _ask(
        AppTest.from_file(APP_FILE, default_timeout=TIMEOUT),
        "Средний чек за 2024 год",
        monkeypatch,
        [refuse_response("Расчёт среднего чека не поддерживается.")],
    )

    assert len(at.exception) == 0
    warnings = " ".join(item.value for item in at.warning)
    assert "Не могу выполнить этот запрос" in warnings
    assert "среднего чека" in warnings
    assert not any("Технические детали" in markdown.value for markdown in at.markdown)


def test_app_clarification_keeps_current_ux(
    llm_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Уточнение агента рисуется как раньше: info + варианты ответа."""
    at = _ask(
        AppTest.from_file(APP_FILE, default_timeout=TIMEOUT),
        "Сравни продажи",
        monkeypatch,
        [clarify_response(["period1", "period2"], "За какие два периода сравнить?")],
    )

    assert len(at.exception) == 0
    infos = " ".join(item.value for item in at.info)
    assert "Нужно уточнение" in infos
    assert "два периода" in infos
