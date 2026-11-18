"""LangGraph-агент: planning-путь для ``use_llm=True``.

Пакет не импортирует Streamlit и не знает про UI: вход — :class:`AgentState`,
выход — состояние с :class:`~onec_analyst.llm_agent.AgentDecision`. Контракт
7 интентов, Pydantic-модели и ``dispatch_intent`` не меняются (ADR 0013).

Модули:

* :mod:`state` — состояние графа, статусы и guard лимита LLM;
* :mod:`prompts` — три промпта (agent_decision, resolve_context, fix_dsl);
* :mod:`tools` — 7 бизнес-инструментов + ``clarify``/``refuse``;
* :mod:`nodes` — узлы графа;
* :mod:`graph` — сборка ``StateGraph`` и :func:`run_agent_graph`.
"""

from __future__ import annotations

__all__ = ["run_agent_graph"]


def __getattr__(name: str):  # pragma: no cover - ленивый реэкспорт
    """Ленивый реэкспорт :func:`run_agent_graph` (не тянуть langgraph при импорте)."""
    if name == "run_agent_graph":
        from onec_analyst.agent.graph import run_agent_graph

        return run_agent_graph
    raise AttributeError(name)
