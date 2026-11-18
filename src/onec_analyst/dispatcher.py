"""Диспетчер запроса: вопрос → план → защита → расчёт → сводка.

Единая точка входа для Streamlit и LangGraph. Модуль не импортирует
Streamlit: UI получает :class:`AssistantResult` и сам решает, что рисовать.
Офлайн-путь (``use_llm=False``) не меняется с этапа 1; агентный путь
(``use_llm=True``) идёт через :func:`onec_analyst.agent.graph.run_agent_graph`
и возвращает те же ``clarify``/``refuse``/``error``/``fallback`` статусы
(см. ADR 0013, этап 2).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any, Sequence

import pandas as pd

from onec_analyst.llm_agent import (
    AgentDecision,
    ClarificationPlan,
    LLMPlanner,
    LLMSettings,
    RuleBasedPlanner,
    guard_decision,
    settings_from_env,
)
from onec_analyst.pipeline import ExecutionResult, execute, extract_entities
from onec_analyst.session import ConversationState
from onec_analyst.summary import SummaryPayload


class DispatchError(ValueError):
    """Ошибка ``dispatch_intent`` с сохранённым решением для UI.

    UI показывает то же сообщение, что и раньше (``Детали: …``), но получает и
    DSL/интент, чтобы отрисовать «Технические детали». Ошибка не глотается;
    класс совместим с ``ValueError``, который исторически ловил ``app.py``.
    """

    def __init__(self, decision: AgentDecision, original: BaseException) -> None:
        super().__init__(str(original))
        self.decision = decision
        self.original = original


@dataclass(frozen=True)
class AssistantResult:
    """Результат обработки вопроса для UI.

    Attributes
    ----------
    ok:
        Удалось ли получить исполнимый DSL (как ``AgentDecision.ok``).
    intent:
        Имя интента или ``clarification``.
    params:
        Параметры DSL.
    dsl:
        JSON-безопасный DSL для «Технических деталей».
    clarification:
        Уточняющий вопрос планировщика, если он был.
    error:
        Дружелюбное описание ошибки планирования.
    result:
        DataFrame результата; ``None`` для уточнения/ошибки.
    summary:
        Готовая текстовая сводка (``payload.text``).
    payload:
        Структурированная сводка для eval/факт-чека.
    trace:
        Шаги текущего запроса (``plan``, ``guard``, ``dispatch``, ``summary``).
    source:
        ``"llm"`` или ``"rules"`` — чем получен DSL после защиты.
    raw_response:
        Сырой ответ модели (для блока «Технические детали»).
    llm_success:
        Был ли успешный вызов LLM **до** ``guard_decision``: лимит считается
        по факту вызова, даже если защита заменила решение правилами.
    status:
        Явный статус агентного решения: ``ok`` | ``clarify`` | ``refuse`` |
        ``error`` | ``fallback`` (ADR 0013). Аддитивное поле: офлайн-путь
        отдаёт ``ok``/``clarify`` как раньше.
    refusal_reason:
        Причина отказа для ``status="refuse"``; для остальных статусов — ``None``.
    """

    ok: bool
    intent: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    dsl: dict[str, Any] = field(default_factory=dict)
    clarification: ClarificationPlan | None = None
    error: str | None = None
    result: pd.DataFrame | None = None
    summary: str = ""
    payload: SummaryPayload | None = None
    trace: tuple[str, ...] = ()
    source: str = "rules"
    raw_response: str = ""
    llm_success: bool = False
    status: str = "ok"
    refusal_reason: str | None = None

    def as_decision(self) -> AgentDecision:
        """Восстановить :class:`AgentDecision` для отрисовки результата."""
        return AgentDecision(
            ok=self.ok,
            intent=self.intent,
            params=dict(self.params),
            clarification=self.clarification,
            error=self.error,
            raw_response=self.raw_response,
            dsl=dict(self.dsl),
            source=self.source,
            status=self.status,
            refusal_reason=self.refusal_reason,
        )


def select_planner(
    use_llm: bool,
    settings: LLMSettings,
) -> LLMPlanner | RuleBasedPlanner:
    """Выбрать планировщик: LLM при явном включении и настроенном провайдере.

    ``use_llm`` не может включить LLM, если она не настроена: без ключа
    приложение остаётся в офлайн-режиме и не делает сетевых вызовов.
    """
    if use_llm and settings.is_configured:
        return LLMPlanner(settings)
    return RuleBasedPlanner()


def _mark(
    session: ConversationState,
    steps: list[str],
    step: str,
) -> None:
    """Записать шаг и в результат запроса, и в память диалога."""
    steps.append(step)
    session.record_trace(step)


def process_request(
    question: str,
    use_llm: bool,
    session: ConversationState,
    df: pd.DataFrame,
    profile: dict[str, Any] | None,
    settings: LLMSettings | None = None,
    today: date | None = None,
    history: Sequence[dict[str, str]] | None = None,
    llm_guard: Any = None,
) -> AssistantResult:
    """Обработать вопрос: план, защита, расчёт и сводка.

    Parameters
    ----------
    question:
        Вопрос пользователя.
    use_llm:
        Флаг «Использовать LLM» (может быть выключен лимитом).
    session:
        Память диалога; обновляется при успешном результате.
    df:
        Очищенная витрина.
    profile:
        Профиль данных для системного промпта.
    settings:
        Настройки LLM; по умолчанию — из окружения.
    today:
        Дата для относительных периодов правил и ``guard_decision``.
    history:
        История сообщений для промпта (в UI — ``st.session_state["history"]``).
    llm_guard:
        Объект с ``before_call``/``after_success`` (протокол
        :class:`onec_analyst.agent.state.LLMCallGuard`). Используется только
        агентным путём: при исчерпании лимита граф уходит в fallback.

    Returns
    -------
    AssistantResult
        Уточнение/отказ/ошибка возвращаются без расчёта; для валидного DSL —
        DataFrame и сводка.

    Raises
    ------
    DispatchError
        Если ``dispatch_intent`` отверг параметры: UI показывает сообщение.

    Notes
    -----
    При ``use_llm=False`` граф не вызывается вообще: решение даёт
    :class:`RuleBasedPlanner`, исполняет его общий пайплайн. Агентный путь
    включается, только когда выбран реальный :class:`LLMPlanner`.
    """
    settings = settings or settings_from_env()
    steps: list[str] = []
    planner = select_planner(use_llm, settings)
    use_graph = use_llm and isinstance(planner, LLMPlanner)

    if use_graph:  # агентный путь: один запуск графа на запрос
        from onec_analyst.agent.graph import run_agent_graph
        from onec_analyst.agent.state import initial_state

        state = initial_state(
            question,
            previous_dsl=session.previous_dsl,
            result_entities=session.result_entities,
            today=today,
            clarification_rounds=session.clarification_rounds,
            profile=profile,
            planner=RuleBasedPlanner(),
            guard=llm_guard,
        )
        final_state = run_agent_graph(state, settings=settings, guard=llm_guard)
        decision: AgentDecision = final_state.get("decision") or AgentDecision(
            ok=False,
            status="error",
            error=str(final_state.get("error") or "Граф завершился без решения"),
            source="rules",
        )
        steps.extend(str(step) for step in final_state.get("trace") or ())
        for step in steps:
            session.record_trace(step)
        llm_success = bool(final_state.get("llm_success"))
    else:
        if isinstance(planner, RuleBasedPlanner):
            decision = planner.plan(question, history, profile, today=today)
        else:
            decision = planner.plan(question, history, profile)
        _mark(session, steps, "plan")
        llm_success = decision.source == "llm" and decision.ok

    # Лимит LLM считается по факту успешного вызова, ещё до защиты от подмены.
    # В агентном пути защита нужна только для исполнимого решения: отказ и
    # уточнение не подменяются правилами.
    if not use_graph or decision.status in ("ok", "fallback"):
        decision = guard_decision(decision, question, today=today)
    if not decision.ok and decision.status == "ok":
        # Офлайн-путь исторически оставлял status="ok" у ошибки: выравниваем
        # явный статус, чтобы UI и тесты различали error от ok/clarify.
        decision = replace(decision, status="error")
    _mark(session, steps, "guard")
    if decision.source == "llm" and decision.ok:
        llm_success = True

    base = AssistantResult(
        ok=decision.ok,
        intent=decision.intent,
        params=dict(decision.params or {}),
        dsl=dict(decision.dsl or {}),
        clarification=decision.clarification,
        error=decision.error,
        result=None,
        summary="",
        payload=None,
        trace=tuple(steps),
        source=decision.source,
        raw_response=decision.raw_response,
        llm_success=llm_success,
        status=decision.status,
        refusal_reason=decision.refusal_reason,
    )

    if (
        decision.intent == "clarification"
        or not decision.ok
        or decision.status in ("clarify", "refuse", "error")
    ):
        return base

    try:
        execution: ExecutionResult = execute(decision, df, profile)
    except (ValueError, TypeError, KeyError) as exc:
        raise DispatchError(decision, exc) from exc

    _mark(session, steps, "dispatch")
    entities = extract_entities(decision, execution.result)
    session.record_result(decision, entities)
    _mark(session, steps, "summary")

    return AssistantResult(
        ok=True,
        intent=decision.intent,
        params=dict(decision.params or {}),
        dsl=dict(decision.dsl or {}),
        clarification=None,
        error=None,
        result=execution.result,
        summary=execution.summary,
        payload=execution.summary_payload,
        trace=tuple(steps),
        source=decision.source,
        raw_response=decision.raw_response,
        llm_success=llm_success,
        status=decision.status,
        refusal_reason=decision.refusal_reason,
    )
