"""Сборка ``StateGraph`` и запуск агента без Streamlit.

Маршрутизация повторяет ТЗ 6.4 §2.4: контекст (если он нужен) → решение →
валидация → не более одной починки → терминальный статус. Узел
``validate_dsl`` не исполняет расчёт: DataFrame и сводку строит вызывающий код
через :func:`onec_analyst.pipeline.execute`.
"""

from __future__ import annotations

from typing import Any, Callable

from langgraph.graph import END, START, StateGraph

from onec_analyst import llm_agent
from onec_analyst.agent import nodes
from onec_analyst.agent.llm import make_agent_client
from onec_analyst.agent.state import (
    AgentState,
    DecisionStatus,
    UnlimitedGuard,
    status_of,
)
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


def build_graph() -> Any:
    """Собрать и скомпилировать ``StateGraph(AgentState)``."""
    builder = StateGraph(AgentState)
    builder.add_node("resolve_context", nodes.resolve_context)
    builder.add_node("agent_decision", nodes.agent_decision)
    builder.add_node("validate_dsl", nodes.validate_dsl)
    builder.add_node("fix_dsl", nodes.fix_dsl)
    builder.add_node("clarify", nodes.clarify)
    builder.add_node("refuse", nodes.refuse)
    builder.add_node("handle_error", nodes.handle_error)

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
    if client is None:
        current = settings or llm_agent.settings_from_env()
        client = make_agent_client(current)
    if on_llm_call is not None:
        client = _OnCallClient(client, on_llm_call)
    prepared["client"] = client
    prepared["planner"] = planner or prepared.get("planner") or RuleBasedPlanner()
    prepared["guard"] = guard or prepared.get("guard") or UnlimitedGuard()
    prepared.setdefault("trace", [])
    prepared.setdefault("retry_count", 0)
    prepared.setdefault("clarification_rounds", 0)
    prepared.setdefault("llm_calls", 0)
    prepared.setdefault("llm_success", False)

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
        return fallback_state
    result: AgentState = dict(final)  # type: ignore[assignment]
    if result.get("decision") is None and not result.get("error"):
        result["decision"] = AgentDecision(
            ok=False,
            status="error",
            error="Граф завершился без решения",
            source="rules",
        )
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
