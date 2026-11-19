"""Узлы агента: контекст, валидация, починка, уточнение, отказ, ошибки."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from onec_analyst.agent import nodes
from onec_analyst.agent.state import CallBudgetGuard, initial_state
from onec_analyst.llm_agent import AgentDecision, ClarificationPlan

from tests.agent_support import (
    FakeLLMClient,
    clarify_response,
    json_response,
    refuse_response,
    text_response,
    tool_response,
)

TODAY = date(2026, 11, 18)


def _state(**kwargs: Any) -> dict[str, Any]:
    """Начальное состояние с фиксированной датой и нужными полями."""
    base = initial_state(kwargs.pop("question", "Сколько мы продали за 2024 год?"), today=TODAY)
    base.update(kwargs)
    return base


# --------------------------------------------------------------------------- #
# resolve_context
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "question",
    [
        "А что с ними?",
        "Покажи этих товаров больше",
        "Те же данные, но за квартал",
        "А теперь покажи топ-3",
        "То же самое, но по неделям",
        "За тот же период по группам",
    ],
)
def test_context_triggers_are_detected(question: str) -> None:
    """Местоимения, эллипсисы и ссылки на период включают контекст."""
    assert nodes.has_context_triggers(question) is True


def test_plain_question_has_no_triggers() -> None:
    """Обычный вопрос не требует памяти диалога."""
    assert nodes.has_context_triggers("Сколько мы продали за 2024 год?") is False


def test_resolve_context_without_triggers_keeps_question(question: str = "Итог за 2024") -> None:
    """Без триггеров узел не меняет вопрос (и не зовёт LLM)."""
    client = FakeLLMClient([text_response("не должно использоваться")])
    state = _state(question=question, client=client, previous_dsl={"intent": "total", "params": {}})

    update = nodes.resolve_context(state)  # type: ignore[arg-type]

    assert update["resolved_question"] == question
    assert update["inherited"] == {}
    assert client.calls == 0


def test_resolve_context_inherits_dates_and_filters() -> None:
    """Даты и фильтры прошлого шага детерминированно попадают в вопрос."""
    previous = {
        "intent": "timeseries",
        "params": {
            "freq": "M",
            "metric": "revenue",
            "start_date": "2023-01-01",
            "end_date": "2023-12-31",
            "product_filter": "Крупа",
        },
    }
    state = _state(question="А теперь то же самое", previous_dsl=previous)

    update = nodes.resolve_context(state)  # type: ignore[arg-type]

    resolved = update["resolved_question"]
    assert "2023-01-01" in resolved and "2023-12-31" in resolved
    assert "product_filter" in resolved and "Крупа" in resolved
    assert update["inherited"]["start_date"] == "2023-01-01"
    assert update["inherited"]["freq"] == "M"


def test_resolve_context_inherits_period_pair() -> None:
    """У прошлого шага сравнения наследуются оба периода и поздний — как границы."""
    previous = {
        "intent": "counterparty_dynamics",
        "params": {
            "period1": ["2023-01-01", "2023-12-31"],
            "period2": ["2024-01-01", "2024-12-31"],
        },
    }
    inherited = nodes.inherit_from_previous(previous, [], "а теперь по группам")

    assert inherited["period1"] == ("2023-01-01", "2023-12-31")
    assert inherited["period2"] == ("2024-01-01", "2024-12-31")
    assert inherited["start_date"] == "2024-01-01"
    assert inherited["end_date"] == "2024-12-31"


def test_resolve_context_substitutes_entities() -> None:
    """Сущности результата подставляются в текст, если вопрос на них ссылается."""
    previous = {
        "intent": "top_bottom_products",
        "params": {"start_date": "2024-10-01", "end_date": "2024-12-31"},
    }
    state = _state(
        question="А как они соотносятся?",
        previous_dsl=previous,
        result_entities=["Колонка Му", "Клавиатура Lambda"],
    )

    update = nodes.resolve_context(state)  # type: ignore[arg-type]

    assert "Колонка Му" in update["resolved_question"]
    assert "Клавиатура Lambda" in update["resolved_question"]
    assert update["inherited"]["entities"] == ["Колонка Му", "Клавиатура Lambda"]


def test_resolve_context_overrides_granularity() -> None:
    """«по неделям» в новом вопросе меняет унаследованную гранулярность."""
    previous = {
        "intent": "timeseries",
        "params": {"freq": "M", "start_date": "2023-01-01", "end_date": "2023-12-31"},
    }
    update = nodes.resolve_context(_state(question="А теперь то же самое, но по неделям", previous_dsl=previous))  # type: ignore[arg-type]

    assert update["inherited"]["freq"] == "W"
    assert 'freq="W"' in update["resolved_question"]


def test_resolve_context_llm_fallback_parses_resolved_question() -> None:
    """LLM-fallback возвращает переформулировку и учитывает вызов в guard."""
    from onec_analyst.llm_agent import CompletionResult

    client = FakeLLMClient(
        [CompletionResult(content='{"resolved_question": "Покажи итог за 2023 год"}')]
    )
    guard = CallBudgetGuard()
    state = _state(
        question="Покажи то же за тот же период",
        previous_dsl={"intent": "total", "params": {}},
        client=client,
        guard=guard,
    )

    update = nodes.resolve_context(state)  # type: ignore[arg-type]

    assert update["resolved_question"] == "Покажи итог за 2023 год"
    assert update["llm_success"] is True
    assert guard.used == 1


# --------------------------------------------------------------------------- #
# validate_dsl
# --------------------------------------------------------------------------- #
def test_validate_dsl_normalizes_arguments() -> None:
    """Валидный DSL нормализуется Pydantic-контрактом и получает status=ok."""
    decision = AgentDecision(
        ok=True,
        intent="total",
        params={"start_date": "2024-01-01", "end_date": "2024-12-31"},
        dsl={"intent": "total", "params": {"start_date": "2024-01-01", "end_date": "2024-12-31"}},
        source="llm",
    )
    update = nodes.validate_dsl(_state(decision=decision))  # type: ignore[arg-type]

    assert update["validation_error"] is None
    normalized = update["decision"]
    assert normalized.status == "ok"
    assert normalized.params["start_date"] == date(2024, 1, 1)
    assert normalized.source == "llm"


def test_validate_dsl_reports_pydantic_error() -> None:
    """Лишнее поле запрещено контрактом: ошибка уходит в validation_error."""
    decision = AgentDecision(
        ok=True,
        intent="total",
        params={"unknown": 1},
        dsl={"intent": "total", "params": {"unknown": 1}},
        source="llm",
    )
    update = nodes.validate_dsl(_state(decision=decision))  # type: ignore[arg-type]

    assert "unknown" in update["validation_error"]


def test_validate_dsl_skips_non_executable_statuses() -> None:
    """Уточнение и отказ не валидируются как DSL."""
    decision = AgentDecision(ok=True, status="clarify", intent="clarification")
    update = nodes.validate_dsl(_state(decision=decision))  # type: ignore[arg-type]

    assert "validation_error" not in update
    assert update["trace"][-1] == "validate_dsl"


# --------------------------------------------------------------------------- #
# fix_dsl
# --------------------------------------------------------------------------- #
def test_fix_dsl_repairs_arguments_once() -> None:
    """Одна починка: JSON аргументов + фиксированный интент."""
    decision = AgentDecision(
        ok=True,
        intent="total",
        params={"start_date": 2024},
        dsl={"intent": "total", "params": {"start_date": 2024}},
        source="llm",
    )
    client = FakeLLMClient(
        [
            type(
                "Result",
                (),
                {"content": '{"start_date": "2024-01-01", "end_date": "2024-12-31"}'},
            )()
        ]
    )
    state = _state(
        decision=decision,
        validation_error="start_date: Input should be a valid date",
        client=client,
        guard=CallBudgetGuard(),
    )

    update = nodes.fix_dsl(state)  # type: ignore[arg-type]

    assert update["retry_count"] == 1
    assert update["validation_error"] is None
    assert update["decision"].params["start_date"] == date(2024, 1, 1)


def test_fix_dsl_second_failure_keeps_error() -> None:
    """Невалидный ответ починки — ошибка, третьей попытки нет."""
    decision = AgentDecision(ok=True, intent="total", params={"bad": 1}, source="llm")
    client = FakeLLMClient([text_response("не JSON")])
    state = _state(decision=decision, validation_error="bad", client=client)

    update = nodes.fix_dsl(state)  # type: ignore[arg-type]

    assert update["retry_count"] == 1
    assert update["validation_error"]


def test_fix_dsl_respects_exhausted_guard() -> None:
    """При исчерпанном лимите починка не делается."""
    decision = AgentDecision(ok=True, intent="total", params={}, source="llm")
    guard = CallBudgetGuard(limit=0)
    client = FakeLLMClient([text_response("{}")])
    state = _state(decision=decision, validation_error="bad", client=client, guard=guard)

    update = nodes.fix_dsl(state)  # type: ignore[arg-type]

    assert client.calls == 0
    assert "Лимит" in update["error"]


# --------------------------------------------------------------------------- #
# clarify / refuse / handle_error
# --------------------------------------------------------------------------- #
def test_clarify_keeps_plan_and_status() -> None:
    """Детерминированное уточнение сохраняет вопрос и missing модели."""
    plan = ClarificationPlan(question="За какой период сравнить?", missing=["period2"])
    decision = AgentDecision(
        ok=True, intent="clarification", clarification=plan, status="clarify"
    )
    update = nodes.clarify(_state(decision=decision))  # type: ignore[arg-type]

    assert update["decision"].status == "clarify"
    assert update["decision"].clarification.missing == ["period2"]


def test_clarify_after_limit_forces_interpretation() -> None:
    """После двух кругов уточнений — детерминированная интерпретация."""
    update = nodes.clarify(
        _state(  # type: ignore[arg-type]
            question="Сколько мы продали за 2024 год?",
            clarification_rounds=2,
        )
    )

    assert update["decision"].status == "ok"
    assert update["decision"].intent == "total"


def test_clarify_after_limit_without_rules_is_error() -> None:
    """Если правила тоже не дали интента — честная ошибка."""
    update = nodes.clarify(
        _state(question="Расскажи что-нибудь", clarification_rounds=2)  # type: ignore[arg-type]
    )

    assert update["decision"].status == "error"
    assert update["decision"].ok is False


def test_refuse_keeps_reason() -> None:
    """Причина отказа обязательна и доходит до решения."""
    decision = AgentDecision(ok=False, status="refuse", refusal_reason="Нет данных")
    update = nodes.refuse(_state(decision=decision))  # type: ignore[arg-type]

    assert update["decision"].status == "refuse"
    assert update["decision"].refusal_reason == "Нет данных"
    assert update["decision"].ok is False


def test_refuse_without_decision_has_default_reason() -> None:
    """Отказ без решения получает объяснение по умолчанию."""
    update = nodes.refuse(_state())  # type: ignore[arg-type]

    assert update["decision"].refusal_reason


def test_handle_error_falls_back_to_rules() -> None:
    """Техническая ошибка при разборчивом вопросе → fallback в правила."""
    update = nodes.handle_error(
        _state(  # type: ignore[arg-type]
            question="Сколько мы продали за 2024 год?", error="сбой"
        )
    )

    assert update["decision"].status == "fallback"
    assert update["decision"].intent == "total"
    assert update["decision"].ok is True


def test_handle_error_without_rules_is_error() -> None:
    """Если правила не разобрали вопрос — честная ошибка, без «похожего» ответа."""
    update = nodes.handle_error(_state(question="Расскажи анекдот", error="сбой"))  # type: ignore[arg-type]

    assert update["decision"].status == "error"
    assert "сбой" in (update["decision"].error or "")


# --------------------------------------------------------------------------- #
# agent_decision
# --------------------------------------------------------------------------- #
def test_agent_decision_normalizes_tool_call() -> None:
    """Один tool call → AgentDecision(status=ok) с аргументами."""
    client = FakeLLMClient([json_response("total", {"start_date": "2024-01-01"})])
    guard = CallBudgetGuard()
    update = nodes.agent_decision(_state(client=client, guard=guard))  # type: ignore[arg-type]

    assert update["decision"].status == "ok"
    assert update["decision"].intent == "total"
    assert update["decision"].params == {"start_date": "2024-01-01"}
    assert guard.used == 1
    assert update["llm_success"] is True


def test_agent_decision_drops_empty_optional_filter() -> None:
    """GigaChat иногда шлёт product_filter="" — это «фильтра нет», не ошибка."""
    client = FakeLLMClient(
        [
            json_response(
                "timeseries",
                {
                    "freq": "M",
                    "start_date": "2024-01-01",
                    "end_date": "2024-12-31",
                    "product_filter": "",
                },
            )
        ]
    )
    update = nodes.agent_decision(_state(client=client))  # type: ignore[arg-type]

    assert update["decision"].status == "ok"
    assert "product_filter" not in update["decision"].params
    assert "product_filter" not in update["decision"].dsl["params"]


def test_agent_decision_turns_generic_sales_clarify_into_total() -> None:
    """«Покажи продажи 2025»: слабая модель переспрашивает — отвечаем total."""
    client = FakeLLMClient(
        [clarify_response(["intent"], "Что именно показать за 2025?")]
    )
    update = nodes.agent_decision(
        _state(question="покажи продажи 2025", client=client)  # type: ignore[arg-type]
    )

    decision = update["decision"]
    assert decision.status == "ok"
    assert decision.intent == "total"
    assert decision.params["start_date"] == date(2025, 1, 1)
    assert decision.params["end_date"] == date(2025, 12, 31)


def test_agent_decision_uses_native_tools_for_gigachat() -> None:
    """Нативный клиент получает tools, JSON-провайдер — нет."""
    native = FakeLLMClient([tool_response("total", {})], supports_tools=True)
    nodes.agent_decision(_state(client=native))  # type: ignore[arg-type]
    assert native.requests[0]["tools"]
    assert any(tool["function"]["name"] == "refuse" for tool in native.requests[0]["tools"])

    json_client = FakeLLMClient([json_response("total", {})])
    nodes.agent_decision(_state(client=json_client))  # type: ignore[arg-type]
    assert json_client.requests[0]["tools"] is None
    assert "JSON-адаптер" in json_client.requests[0]["messages"][0]["content"]


def test_agent_decision_without_guard_call_is_error() -> None:
    """Guard, запретивший вызов, не даёт обратиться к провайдеру."""
    client = FakeLLMClient([json_response("total", {})])
    update = nodes.agent_decision(
        _state(client=client, guard=CallBudgetGuard(limit=0))  # type: ignore[arg-type]
    )

    assert client.calls == 0
    assert update["decision"] is None
    assert "Лимит" in update["error"]


@pytest.mark.parametrize(
    "response",
    [text_response("просто текст"), clarify_response([])],
)
def test_agent_decision_rejects_bad_answers(response) -> None:
    """Свободный текст и пустой clarify — ошибка, а не угадывание."""
    update = nodes.agent_decision(_state(client=FakeLLMClient([response])))  # type: ignore[arg-type]

    assert update["decision"] is None
    assert update["validation_error"]


def test_agent_decision_requires_period2_for_compare() -> None:
    """Один период у интента сравнения → clarify(period2), без подстановки."""
    client = FakeLLMClient(
        [json_response("group_dynamics", {"period1": ["2023-01-01", "2023-12-31"]})]
    )
    update = nodes.agent_decision(_state(client=client))  # type: ignore[arg-type]

    decision = update["decision"]
    assert decision.status == "clarify"
    assert decision.clarification.missing == ["period2"]


def test_agent_decision_clarifies_when_periods_are_equal() -> None:
    """period1 == period2 (скопирован) — сравнение периода с собой → clarify (009)."""
    client = FakeLLMClient(
        [
            json_response(
                "group_dynamics",
                {
                    "period1": ["2023-01-01", "2023-12-31"],
                    "period2": ["2023-01-01", "2023-12-31"],
                },
            )
        ]
    )
    update = nodes.agent_decision(_state(client=client))  # type: ignore[arg-type]

    decision = update["decision"]
    assert decision.status == "clarify"
    assert decision.clarification.missing == ["period2"]


def test_agent_decision_refuses_future_period() -> None:
    """Будущий период — отказ: данных ещё нет."""
    client = FakeLLMClient(
        [json_response("total", {"start_date": "2030-01-01", "end_date": "2030-12-31"})]
    )
    update = nodes.agent_decision(_state(client=client))  # type: ignore[arg-type]

    assert update["decision"].status == "refuse"
    assert "будущ" in update["decision"].refusal_reason.lower()


def test_total_with_future_end_but_past_start_is_allowed() -> None:
    """«Этот месяц»: конец месяца позже today, но начало — нет => не отказ (017)."""
    client = FakeLLMClient(
        [json_response("total", {"start_date": "2024-05-01", "end_date": "2024-05-31"})]
    )
    state = initial_state("Сколько мы заработали в этом месяце?", today=date(2024, 5, 15))
    state.update({"client": client, "guard": CallBudgetGuard(3)})
    update = nodes.agent_decision(state)  # type: ignore[arg-type]

    assert update["decision"].status == "ok"
    assert update["decision"].intent == "total"


def test_agent_decision_refuses_vat() -> None:
    """Выручка с НДС вне MVP: отказ с объяснением."""
    client = FakeLLMClient(
        [json_response("total", {"start_date": "2024-01-01", "end_date": "2024-12-31"})]
    )
    update = nodes.agent_decision(  # type: ignore[arg-type]
        _state(question="Выручка с НДС за 2024 год", client=client)
    )

    assert update["decision"].status == "refuse"
    assert "НДС" in update["decision"].refusal_reason


def test_agent_decision_allows_revenue_without_vat() -> None:
    """«Без НДС» — это штатная метрика, а не запрос про revenue_vat."""
    client = FakeLLMClient(
        [json_response("total", {"start_date": "2024-01-01", "end_date": "2024-12-31"})]
    )
    update = nodes.agent_decision(  # type: ignore[arg-type]
        _state(question="Выручка без НДС за 2024 год", client=client)
    )

    assert update["decision"].status == "ok"


def test_agent_decision_does_not_refuse_compare_of_tops_with_memory() -> None:
    """Память о товарах не мешает обычному сравнению топов."""
    client = FakeLLMClient(
        [
            json_response(
                "compare_top_products",
                {
                    "period1": ["2023-01-01", "2023-12-31"],
                    "period2": ["2024-01-01", "2024-12-31"],
                    "top_n": 5,
                },
            )
        ]
    )
    update = nodes.agent_decision(  # type: ignore[arg-type]
        _state(
            question="Сравни топ-5 товаров за 2023 и 2024 годы",
            result_entities=["Колонка Му", "Клавиатура Lambda"],
            client=client,
        )
    )

    assert update["decision"].status == "ok"


def test_agent_decision_refuses_compare_of_named_products() -> None:
    """Сравнение конкретных товаров из прошлого результата — отказ."""
    previous = {
        "intent": "top_bottom_products",
        "params": {"start_date": "2024-10-01", "end_date": "2024-12-31"},
    }
    client = FakeLLMClient(
        [
            json_response(
                "compare_top_products",
                {
                    "period1": ["2024-07-01", "2024-09-30"],
                    "period2": ["2024-10-01", "2024-12-31"],
                },
            )
        ]
    )
    state = _state(
        question="А как они соотносятся с прошлым кварталом?",
        previous_dsl=previous,
        result_entities=["Колонка Му", "Клавиатура Lambda"],
        client=client,
    )
    state.update(nodes.resolve_context(state))  # type: ignore[arg-type]
    update = nodes.agent_decision(state)  # type: ignore[arg-type]

    assert update["decision"].status == "refuse"
    assert "не поддерживается" in update["decision"].refusal_reason
    assert "сравнен" in update["decision"].refusal_reason.lower()


def test_agent_decision_llm_error_is_error() -> None:
    """Сетевая ошибка провайдера не превращается в решение."""
    from onec_analyst.llm_agent import LLMError

    class Broken(FakeLLMClient):
        def complete(self, messages, tools=None):  # type: ignore[override]
            raise LLMError("нет сети")

    update = nodes.agent_decision(_state(client=Broken([text_response("x")])))  # type: ignore[arg-type]

    assert update["decision"] is None
    assert "LLM недоступна" in update["error"]


def test_agent_decision_against_refuse_reason() -> None:
    """Отказ модели без причины отклоняется на уровне аргументов."""
    update = nodes.agent_decision(
        _state(client=FakeLLMClient([refuse_response(" ")])),  # type: ignore[arg-type]
    )

    assert update["decision"] is None
    assert "причина" in update["validation_error"].lower()
