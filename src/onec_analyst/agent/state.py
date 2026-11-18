"""Состояние LangGraph-агента, статусы решения и guard лимита LLM.

Здесь нет ни Streamlit, ни расчётов: только типы, которые передаются между
узлами графа. Поля соответствуют ТЗ 6.4 §2.1; аддитивные служебные поля
(``client``, ``planner``, ``guard``, ``profile``, ``llm_success``, ``llm_calls``)
не являются частью доменного состояния и нужны, чтобы граф был тестируемым без
глобальных переменных.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any, Protocol, TypedDict

from onec_analyst.llm_agent import AgentDecision, RuleBasedPlanner

#: Сколько уточнений подряд допустимо до принудительной интерпретации.
MAX_CLARIFICATIONS = 2
#: Жёсткий предел LLM-вызовов на один запрос (resolve_context + agent_decision + fix_dsl).
MAX_LLM_CALLS = 3


class DecisionStatus(str, Enum):
    """Явный статус решения агента (ТЗ 6.4 §2.2, ADR 0013 D2)."""

    OK = "ok"
    CLARIFY = "clarify"
    REFUSE = "refuse"
    ERROR = "error"
    FALLBACK = "fallback"


def status_of(decision: AgentDecision | None) -> DecisionStatus:
    """Статус решения; отсутствующее решение — ``error``, неизвестная строка — ошибка."""
    if decision is None:
        return DecisionStatus.ERROR
    try:
        return DecisionStatus(str(decision.status))
    except ValueError:
        return DecisionStatus.ERROR


class AgentState(TypedDict, total=False):
    """Состояние графа (ТЗ 6.4 §2.1) плюс служебные поля запуска.

    Доменные поля: ``question``, ``previous_dsl``, ``result_entities``,
    ``today``, ``resolved_question``, ``decision``, ``validation_error``,
    ``retry_count``, ``clarification_rounds``, ``trace``, ``error``.

    Служебные: ``client`` (LLM-клиент), ``planner`` (правила для fallback),
    ``guard`` (лимит вызовов), ``profile`` (профиль витрины для промпта),
    ``inherited`` (что унаследовано из ``previous_dsl``), ``llm_success`` и
    ``llm_calls`` (учёт для диспетчера и UI).
    """

    question: str
    previous_dsl: dict[str, Any] | None
    result_entities: list[str]
    today: str
    resolved_question: str
    decision: AgentDecision | None
    validation_error: str | None
    retry_count: int
    clarification_rounds: int
    trace: list[str]
    error: str | None

    client: Any
    planner: RuleBasedPlanner
    guard: "LLMCallGuard"
    profile: dict[str, Any] | None
    inherited: dict[str, Any]
    llm_success: bool
    llm_calls: int


class LLMCallGuard(Protocol):
    """Учёт лимита LLM на каждый вызов (ТЗ 6.4 §4.2).

    ``before_call`` возвращает ``False``, если вызов делать нельзя; ``after_success``
    вызывается только после успешного ответа провайдера — неудачные сетевые
    вызовы счётчик не съедают.
    """

    def before_call(self) -> bool:  # pragma: no cover - протокол
        ...

    def after_success(self) -> None:  # pragma: no cover - протокол
        ...


class UnlimitedGuard:
    """Guard без лимита: разрешает все вызовы (значение по умолчанию)."""

    def before_call(self) -> bool:
        """Разрешить вызов."""
        return True

    def after_success(self) -> None:
        """Ничего не считать."""


@dataclass
class CallBudgetGuard:
    """Простой счётчик: не больше ``limit`` успешных вызовов на запрос.

    Используется тестами и вызывающим кодом, у которого нет своего лимитера.
    Для UI лимит остаётся в ``rate_limiter``: диспетчер принимает любой объект
    с методами :class:`LLMCallGuard`.
    """

    limit: int = MAX_LLM_CALLS
    used: int = 0

    def before_call(self) -> bool:
        """Разрешён ли ещё один вызов."""
        return self.used < self.limit

    def after_success(self) -> None:
        """Учесть успешный вызов."""
        self.used += 1


def initial_state(
    question: str,
    *,
    previous_dsl: dict[str, Any] | None = None,
    result_entities: list[str] | None = None,
    today: date | None = None,
    clarification_rounds: int = 0,
    profile: dict[str, Any] | None = None,
    client: Any = None,
    planner: RuleBasedPlanner | None = None,
    guard: LLMCallGuard | None = None,
) -> AgentState:
    """Собрать начальное состояние графа из запроса и памяти диалога."""
    current = today or date.today()
    return AgentState(
        question=str(question or "").strip(),
        previous_dsl=dict(previous_dsl) if previous_dsl else None,
        result_entities=list(result_entities or []),
        today=current.isoformat(),
        resolved_question=str(question or "").strip(),
        decision=None,
        validation_error=None,
        retry_count=0,
        clarification_rounds=max(0, int(clarification_rounds)),
        trace=[],
        error=None,
        client=client,
        planner=planner or RuleBasedPlanner(),
        guard=guard or CallBudgetGuard(),
        profile=profile,
        inherited={},
        llm_success=False,
        llm_calls=0,
    )


def mark_step(state: AgentState, step: str) -> list[str]:
    """Вернуть трассировку с добавленным шагом (новый список, без мутации)."""
    return [*state.get("trace", []), step]


def state_today(state: AgentState) -> date:
    """Дата из состояния (``YYYY-MM-DD``); при мусоре — текущая дата."""
    try:
        return date.fromisoformat(str(state.get("today")))
    except (TypeError, ValueError):
        return date.today()


__all__ = [
    "AgentState",
    "CallBudgetGuard",
    "DecisionStatus",
    "LLMCallGuard",
    "MAX_CLARIFICATIONS",
    "MAX_LLM_CALLS",
    "UnlimitedGuard",
    "initial_state",
    "mark_step",
    "state_today",
    "status_of",
]
