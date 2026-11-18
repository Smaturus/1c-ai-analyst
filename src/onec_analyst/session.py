"""Состояние диалога между запросами — без Streamlit и без DataFrame.

``ConversationState`` хранит только то, что нужно для контекста следующего
вопроса: предыдущий DSL, сущности результата, счётчик уточнений и трассировку
шагов. Историю сообщений и виджеты по-прежнему держит Streamlit; здесь нет
ни ``st.session_state``, ни тяжёлых данных.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - только для аннотаций
    from onec_analyst.llm_agent import AgentDecision


@dataclass
class ConversationState:
    """Память диалога для планировщика и будущего LangGraph-графа.

    Attributes
    ----------
    previous_dsl:
        JSON-безопасный DSL предыдущего успешного ответа (не ``AgentDecision``).
    result_entities:
        Сущности последнего результата: товары, контрагенты, группы.
    clarification_rounds:
        Сколько уточнений подряд уже показано. Один счётчик с UI: Streamlit
        синхронизирует его с ``st.session_state["clarification_rounds"]``.
    trace:
        Шаги обработки запросов (``plan``, ``guard``, ``dispatch``, ``summary``).
        Без сырых вопросов и данных — только имена шагов.
    """

    previous_dsl: dict[str, Any] | None = None
    result_entities: list[str] = field(default_factory=list)
    clarification_rounds: int = 0
    trace: list[str] = field(default_factory=list)

    def reset(self) -> None:
        """Сбросить память диалога (новый разговор, «Очистить историю»)."""
        self.previous_dsl = None
        self.result_entities = []
        self.clarification_rounds = 0
        self.trace = []

    def record_result(
        self,
        decision: "AgentDecision",
        entities: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        """Запомнить DSL и сущности успешного результата."""
        self.previous_dsl = dict(decision.dsl or {})
        self.result_entities = list(entities or [])

    def record_trace(self, step: str) -> None:
        """Добавить шаг обработки в трассировку (пустые шаги игнорируются)."""
        text = str(step or "").strip()
        if text:
            self.trace.append(text)
