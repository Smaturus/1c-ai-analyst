"""Сборка ``StateGraph`` и запуск агента без Streamlit.

Маршрутизация повторяет ТЗ 6.4 §2.4: контекст (если он нужен) → решение →
валидация → не более одной починки → терминальный статус. Узел
``validate_dsl`` не исполняет расчёт: DataFrame и сводку строит вызывающий код
через :func:`onec_analyst.pipeline.execute`.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from langgraph.graph import END, START, StateGraph

from onec_analyst import llm_agent
from onec_analyst.agent import nodes
from onec_analyst.agent.audit import audit_event
from onec_analyst.agent.llm import make_agent_client
from onec_analyst.agent.state import (
    AgentState,
    DecisionStatus,
    UnlimitedGuard,
    status_of,
)
from onec_analyst.audit import AuditLog
from onec_analyst.llm_agent import AgentDecision, LLMSettings, RuleBasedPlanner

#: Предел шагов графа: ветвление короткое, лишний шаг — признак цикла.
DEFAULT_RECURSION_LIMIT = 25


def route_entry(state: AgentState) -> str:
    """Старт: контекст нужен только при наличии ``previous_dsl`` и триггеров."""
    question = str(state.get("question") or "")
    if state.get("previous_dsl") and nodes.has_context_triggers(question):
        return "resolve_context"
    return "agent_decision"


def route_decision(state: AgentState) -> str:
    """После ``agent_decision``: ветка по явному статусу решения."""
    decision = state.get("decision")
    status = status_of(decision)
    if decision is None:
        return "handle_error"
    if status is DecisionStatus.OK:
        return "validate_dsl"
    if status is DecisionStatus.CLARIFY:
        return "clarify"
    if status is DecisionStatus.REFUSE:
        return "refuse"
    return "handle_error"


def route_validation(state: AgentState) -> str:
    """После ``validate_dsl``: конец, одна починка или ``handle_error``."""
    if not state.get("validation_error"):
        return END
    if int(state.get("retry_count") or 0) == 0:
        return "fix_dsl"
    return "handle_error"


def _error_type_for(event: str) -> str:
    """Категория ошибки по имени события (ТЗ 6.4 §4.4).

    Не литерал ``"error"``: у каждого события своя зона ответственности, чтобы
    по журналу можно было отличить провал валидации, сбой LLM и непойманную
    ошибку.
    """
    if event in ("validate_fail", "fix_dsl"):
        return "validation"
    if event == "agent_decision":
        return "llm"
    if event == "handle_error":
        return "unhandled"
    return "error"


def _audited_node(name: str, node: Callable[[AgentState], dict[str, Any]]) -> Callable:
    """Обернуть узел: после выполнения записать шаг в аудит.

    Обёртка не меняет решение и не пишет текст вопроса/ответа. Для
    ``validate_dsl`` событие называется ``validate_ok``/``validate_fail``,
    остальные шаги — по имени узла. Пишет ``duration_ms`` (время узла, int мс)
    и, при ``status="fallback"``, дополнительное событие ``fallback``.
    """

    def wrapper(state: AgentState) -> dict[str, Any]:
        started = time.monotonic()
        update = node(state)
        duration_ms = int((time.monotonic() - started) * 1000)
        decision = update["decision"] if "decision" in update else state.get("decision")
        status = status_of(decision)
        event = name
        if name == "validate_dsl":
            event = "validate_fail" if update.get("validation_error") else "validate_ok"
            if update.get("validation_error"):
                status = DecisionStatus.ERROR
        elif name == "fix_dsl" and update.get("validation_error"):
            status = DecisionStatus.ERROR
        elif name == "resolve_context":
            status = DecisionStatus.OK
        fields: dict[str, Any] = {
            "status": status.value,
            "intent": decision.intent if decision is not None else None,
            "retry_count": update.get("retry_count", state.get("retry_count")),
            "llm_calls": update.get("llm_calls", state.get("llm_calls")),
            "fallback": status is DecisionStatus.FALLBACK,
            "duration_ms": duration_ms,
        }
        if name == "clarify" and decision is not None and decision.clarification is not None:
            fields["missing"] = list(decision.clarification.missing)
        if status is DecisionStatus.ERROR:
            fields["error_type"] = _error_type_for(event)
        audit_event(state, event, **fields)
        if status is DecisionStatus.FALLBACK:
            audit_event(
                state,
                "fallback",
                status=DecisionStatus.FALLBACK.value,
                intent=decision.intent if decision is not None else None,
                duration_ms=duration_ms,
            )
        return update

    return wrapper


def build_graph() -> Any:
    """Собрать и скомпилировать ``StateGraph(AgentState)``."""
    builder = StateGraph(AgentState)
    builder.add_node("resolve_context", _audited_node("resolve_context", nodes.resolve_context))
    builder.add_node("agent_decision", _audited_node("agent_decision", nodes.agent_decision))
    builder.add_node("validate_dsl", _audited_node("validate_dsl", nodes.validate_dsl))
    builder.add_node("fix_dsl", _audited_node("fix_dsl", nodes.fix_dsl))
    builder.add_node("clarify", _audited_node("clarify", nodes.clarify))
    builder.add_node("refuse", _audited_node("refuse", nodes.refuse))
    builder.add_node("handle_error", _audited_node("handle_error", nodes.handle_error))

    builder.add_conditional_edges(
        START,
        route_entry,
        {"resolve_context": "resolve_context", "agent_decision": "agent_decision"},
    )
    builder.add_edge("resolve_context", "agent_decision")
    builder.add_conditional_edges(
        "agent_decision",
        route_decision,
        {
            "validate_dsl": "validate_dsl",
            "clarify": "clarify",
            "refuse": "refuse",
            "handle_error": "handle_error",
        },
    )
    builder.add_conditional_edges(
        "validate_dsl",
        route_validation,
        {"fix_dsl": "fix_dsl", "handle_error": "handle_error", END: END},
    )
    builder.add_edge("fix_dsl", "validate_dsl")
    builder.add_edge("clarify", END)
    builder.add_edge("refuse", END)
    builder.add_edge("handle_error", END)
    return builder.compile()


_COMPILED: Any = None


def compiled_graph() -> Any:
    """Скомпилированный граф (кеш на процесс: сборка не зависит от запроса)."""
    global _COMPILED
    if _COMPILED is None:
        _COMPILED = build_graph()
    return _COMPILED


class _OnCallClient:
    """Обёртка клиента: вызывает ``on_llm_call`` после успешного ответа.

    Колбэк не влияет на решение и не получает текст вопроса/ответа — только
    факт вызова (это нужно UI для счётчика и будущего аудита).
    """

    def __init__(self, client: Any, callback: Callable[[], None]) -> None:
        self._client = client
        self._callback = callback

    @property
    def supports_tools(self) -> bool:
        """Пробросить признак нативного tool calling."""
        return bool(getattr(self._client, "supports_tools", False))

    def complete(self, messages: list[dict[str, str]], tools: list[dict[str, Any]] | None = None):
        """Вызвать клиент и отметить успешный вызов."""
        result = self._client.complete(messages, tools=tools)
        self._callback()
        return result

    def chat(self, messages: list[dict[str, str]]) -> str:
        """Текстовый вызов (совместимость с классификатором)."""
        return self.complete(messages, tools=None).content


def run_agent_graph(
    state: AgentState,
    *,
    client: Any = None,
    planner: RuleBasedPlanner | None = None,
    settings: LLMSettings | None = None,
    guard: Any = None,
    audit: AuditLog | None = None,
    on_llm_call: Callable[[], None] | None = None,
    recursion_limit: int = DEFAULT_RECURSION_LIMIT,
) -> AgentState:
    """Запустить граф ровно один раз и вернуть финальное состояние.

    Parameters
    ----------
    state:
        Начальное состояние (:func:`onec_analyst.agent.state.initial_state`).
    client:
        LLM-клиент; если не передан — создаётся из ``settings``.
    planner:
        Правила для ``handle_error``/``force_interpretation``.
    settings:
        Настройки LLM (используются, когда ``client`` не передан).
    guard:
        Объект с ``before_call``/``after_success``. По умолчанию — бюджет
        :class:`~onec_analyst.agent.state.CallBudgetGuard` на три вызова: даже
        без лимитера UI один запрос не сделает больше трёх вызовов LLM.
    on_llm_call:
        Колбэк после каждого успешного вызова LLM.
    recursion_limit:
        Предел шагов графа.

    Notes
    -----
    Падение графа не пробрасывается в UI: возвращается состояние со
    ``status="error"`` и текстом ошибки, чтобы вызывающий код показал честное
    сообщение, а не «похожий» ответ.
    """
    prepared: AgentState = dict(state)  # type: ignore[assignment]
    audit_settings = settings
    if client is None:
        current = settings or llm_agent.settings_from_env()
        audit_settings = current
        client = make_agent_client(current)
    if on_llm_call is not None:
        client = _OnCallClient(client, on_llm_call)
    prepared["client"] = client
    prepared["planner"] = planner or prepared.get("planner") or RuleBasedPlanner()
    prepared["guard"] = guard or prepared.get("guard") or UnlimitedGuard()
    prepared["audit"] = audit or prepared.get("audit") or AuditLog()
    prepared.setdefault("trace", [])
    prepared.setdefault("retry_count", 0)
    prepared.setdefault("clarification_rounds", 0)
    prepared.setdefault("llm_calls", 0)
    prepared.setdefault("llm_success", False)
    audit_event(
        prepared,
        "graph_start",
        provider=audit_settings.provider if audit_settings is not None else None,
        model=audit_settings.model if audit_settings is not None else None,
    )

    graph_started = time.monotonic()
    try:
        final = compiled_graph().invoke(
            prepared,
            config={"recursion_limit": max(2, int(recursion_limit))},
        )
    except Exception as exc:  # граф не должен ронять UI
        fallback_state: AgentState = dict(prepared)  # type: ignore[assignment]
        fallback_state["decision"] = AgentDecision(
            ok=False,
            status="error",
            error=f"Сбой графа: {exc}",
            source="rules",
        )
        fallback_state["error"] = f"Сбой графа: {exc}"
        fallback_state["trace"] = [*prepared.get("trace", []), "graph_error"]
        duration_ms = int((time.monotonic() - graph_started) * 1000)
        audit_event(
            fallback_state,
            "graph_error",
            status="error",
            error_type="error",
            duration_ms=duration_ms,
        )
        audit_event(
            fallback_state,
            "graph_end",
            status="error",
            fallback=False,
            error_type="error",
            duration_ms=duration_ms,
        )
        return fallback_state
    result: AgentState = dict(final)  # type: ignore[assignment]
    if result.get("decision") is None and not result.get("error"):
        result["decision"] = AgentDecision(
            ok=False,
            status="error",
            error="Граф завершился без решения",
            source="rules",
        )
    decision = result.get("decision")
    final_status = status_of(decision)
    graph_end_fields: dict[str, Any] = {
        "status": final_status.value,
        "intent": decision.intent if decision is not None else None,
        "llm_calls": result.get("llm_calls"),
        "retry_count": result.get("retry_count"),
        "fallback": final_status is DecisionStatus.FALLBACK,
        "duration_ms": int((time.monotonic() - graph_started) * 1000),
    }
    if final_status is DecisionStatus.ERROR:
        # Терминальная ошибка обычного графа проходит через handle_error;
        # иначе — сбой сборки или синтетическое «без решения» (прочее).
        graph_end_fields["error_type"] = (
            "unhandled" if "handle_error" in (result.get("trace") or []) else "error"
        )
    audit_event(result, "graph_end", **graph_end_fields)
    return result


__all__ = [
    "DEFAULT_RECURSION_LIMIT",
    "build_graph",
    "compiled_graph",
    "route_decision",
    "route_entry",
    "route_validation",
    "run_agent_graph",
]
