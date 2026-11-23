"""Состояние LangGraph-агента, статусы решения и guard лимита LLM.

Здесь нет ни Streamlit, ни расчётов: только типы, которые передаются между
узлами графа. Поля соответствуют ТЗ 6.4 §2.1; аддитивные служебные поля
(``client``, ``planner``, ``guard``, ``profile``, ``llm_success``, ``llm_calls``)
не являются частью доменного состояния и нужны, чтобы граф был тестируемым без
глобальных переменных.
"""

from __future__ import annotations

from dataclasses import dataclass
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
    guard: LLMCallGuard
    audit: Any
    profile: dict[str, Any] | None
    inherited: dict[str, Any]
    llm_success: bool
    #: Число попыток вызова LLM, включая завершившиеся ошибкой (DECISIONS #24).
    llm_calls: int
    #: Вызов LLM не состоялся по технической причине (сеть/таймаут).
    #: Отличается от лимита: лимит по-прежнему уходит в fallback-правила,
    #: а техническая недоступность даёт честную ошибку (DECISIONS #20).
    llm_unavailable: bool
    #: Текст, отправленный модели (system + user) последним вызовом: только для
    #: диагностики ошибки, в UI «Технические детали».
    llm_request: str
    #: Guard отказал в вызове из-за лимита: это не ошибка LLM и не повод
    #: подставлять правила при включённой галочке (DECISIONS #21).
    limit_blocked: bool
    #: Ответ модели, не прошедший контракт (JSON-DSL): показывается в ошибке
    #: вместо молчаливого перехода на правила (DECISIONS #22).
    invalid_response: str
    #: Каталог сущностей витрины для guard'а фильтров: ключи ``product_filter``/
    #: ``group_filter`` → список фактических имён. None — проверка каталога
    #: пропускается (остаётся только подстрока в вопросе).
    filter_catalog: dict[str, list[str]] | None


class LLMCallGuard(Protocol):
    """Учёт лимита LLM на каждый вызов (ТЗ 6.4 §4.2).

    ``before_call`` возвращает ``False``, если вызов делать нельзя; ``after_success``
    вызывается только после успешного ответа провайдера, ``after_failure`` — после
    сетевого сбоя, чтобы вернуть зарезервированный слот. Неудачные вызовы счётчик
    не съедают.
    """

    def before_call(self) -> bool:  # pragma: no cover - протокол
        ...

    def after_success(self) -> None:  # pragma: no cover - протокол
        ...

    def after_failure(self) -> None:  # pragma: no cover - протокол
        ...


class UnlimitedGuard:
    """Guard без лимита: разрешает все вызовы (значение по умолчанию)."""

    def before_call(self) -> bool:
        """Разрешить вызов."""
        return True

    def after_success(self) -> None:
        """Ничего не считать."""

    def after_failure(self) -> None:
        """Ничего не откатывать."""


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

    def after_failure(self) -> None:
        """Сбой не тратит бюджет (счётчик «used» не рос на ``before_call``)."""


class CompositeGuard:
    """Композитный guard: потолок на запрос + лимит сессии/дня.

    Объединяет :class:`CallBudgetGuard` (жёсткий потолок вызовов на один запрос)
    и лимитер (``RateLimiterGuard``). ``before_call`` проходит по всем внутренним
    guard'ам; если один отказывает, уже сделанные резервы предыдущих откатываются
    через ``after_failure``. ``after_success``/``after_failure`` делегируются всем.
    """

    def __init__(self, *guards: LLMCallGuard) -> None:
        self._guards = list(guards)

    def before_call(self) -> bool:
        reserved: list[LLMCallGuard] = []
        for guard in self._guards:
            if not guard.before_call():
                for item in reserved:
                    item.after_failure()
                return False
            reserved.append(guard)
        return True

    def after_success(self) -> None:
        for guard in self._guards:
            guard.after_success()

    def after_failure(self) -> None:
        for guard in self._guards:
            guard.after_failure()


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
    audit: Any = None,
    filter_catalog: dict[str, list[str]] | None = None,
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
        audit=audit,
        profile=profile,
        inherited={},
        llm_success=False,
        llm_calls=0,
        llm_unavailable=False,
        llm_request="",
        limit_blocked=False,
        invalid_response="",
        filter_catalog=dict(filter_catalog) if filter_catalog else None,
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
    "MAX_CLARIFICATIONS",
    "MAX_LLM_CALLS",
    "AgentState",
    "CallBudgetGuard",
    "CompositeGuard",
    "DecisionStatus",
    "LLMCallGuard",
    "UnlimitedGuard",
    "initial_state",
    "mark_step",
    "state_today",
    "status_of",
]
