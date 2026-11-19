"""Исполнение канонического DSL: валидация → dispatch_intent → сводка.

Слой не знает ни про Streamlit, ни про LLM-провайдеров: на вход приходит
:class:`AgentDecision` (или JSON-DSL), на выходе — DataFrame и
:class:`SummaryPayload`. Ошибки ``dispatch_intent`` не глотаются: вызывающий
код (UI) показывает то же сообщение, что и раньше.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any

import pandas as pd

from onec_analyst.analytics import PRODUCT, PRODUCT_GROUP, dispatch_intent
from onec_analyst.entities import resolve_entity
from onec_analyst.llm_agent import (
    AgentDecision,
    ClarificationPlan,
    parse_plan_json,
    to_dispatch_params,
)
from onec_analyst.summary import (
    SummaryPayload,
    build_summary_payload,
)
from onec_analyst.summary import (
    extract_entities as _extract_entities,
)


@dataclass(frozen=True)
class ExecutionResult:
    """Результат исполнения решения: данные и готовая сводка."""

    decision: AgentDecision
    result: pd.DataFrame
    summary_payload: SummaryPayload

    @property
    def summary(self) -> str:
        """Текст сводки (совместимое имя для UI)."""
        return self.summary_payload.text


def _json_default(value: Any) -> str:
    """Сериализовать даты DSL в ISO-строки для повторной Pydantic-валидации."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return str(value)


#: Текстовые параметры, где пустая строка означает «фильтра нет».
_EMPTY_TEXT_PARAMS = ("product_filter", "group_filter")


def drop_empty_filters(params: dict[str, Any]) -> dict[str, Any]:
    """Убрать пустые ``product_filter``/``group_filter`` из параметров.

    GigaChat иногда присылает ``"product_filter": ""`` для запроса без фильтра.
    Для аналитики пустая строка — не фильтр, а ``dispatch_intent`` отвергает
    пустые значения. Нормализуем это до Pydantic/dispatch, не подставляя данные.
    """
    return {
        key: value
        for key, value in params.items()
        if not (
            key in _EMPTY_TEXT_PARAMS
            and isinstance(value, str)
            and not value.strip()
        )
    }


def validate_decision(dsl: dict[str, Any]) -> AgentDecision:
    """Канонический JSON-DSL → :class:`AgentDecision` через Pydantic.

    Использует тот же ``parse_plan_json``/``to_dispatch_params``, что и
    планировщик: контракт валидации не дублируется и не ослабляется.
    Пустые текстовые фильтры отбрасываются как «фильтра нет».
    """
    if not isinstance(dsl, dict):
        raise ValueError(f"DSL должен быть объектом JSON (dict), получено {type(dsl)!r}")
    payload = json.dumps(dsl, ensure_ascii=False, default=_json_default)
    plan = parse_plan_json(payload)
    dsl_json = plan.model_dump(mode="json", exclude_none=True)
    if isinstance(plan, ClarificationPlan):
        return AgentDecision(
            ok=True,
            intent="clarification",
            clarification=plan,
            dsl=dsl_json,
            source="rules",
            status="clarify",
        )
    intent, params = to_dispatch_params(plan)
    params = drop_empty_filters(params)
    dsl_json["params"] = drop_empty_filters(dict(dsl_json.get("params") or {}))
    return AgentDecision(
        ok=True,
        intent=intent,
        params=params,
        dsl=dsl_json,
        source="rules",
    )


#: Синоним :func:`validate_decision` для читаемости на границе DSL.
normalize_dsl = validate_decision


def _resolve_decision_filters(decision: AgentDecision, df: pd.DataFrame) -> AgentDecision:
    """Канонизировать ``product_filter``/``group_filter`` по каталогу витрины.

    Идемпотентно: каноническая строка не меняется, повторный резолв — no-op.
    Нерезолвнутая строка остаётся как есть: витрина даст честный пустой
    результат (ADR 0007, зеркальная сторона), а не «похожий» товар.
    """
    params = dict(decision.params or {})
    changed = False
    for key, column in (("product_filter", PRODUCT), ("group_filter", PRODUCT_GROUP)):
        value = params.get(key)
        if not (isinstance(value, str) and value.strip() and column in df.columns):
            continue
        catalog = df[column].dropna().astype("string").unique().tolist()
        resolved = resolve_entity(value.strip(), catalog).value
        if resolved != value:
            params[key] = resolved
            changed = True
    if not changed:
        return decision
    dsl = dict(decision.dsl or {})
    dsl["params"] = params
    return replace(decision, params=params, dsl=dsl)


def execute(
    decision: AgentDecision,
    df: pd.DataFrame,
    profile: dict[str, Any] | None = None,
) -> ExecutionResult:
    """Выполнить интент и собрать сводку; ошибки расчёта не перехватываются.

    Raises
    ------
    ValueError
        Если решение не содержит исполнимого интента (``clarification``,
        ``ok=False``) или ``dispatch_intent`` отверг параметры.
    """
    intent = decision.intent or ""
    if not decision.ok or intent in ("", "clarification"):
        raise ValueError(
            f"Решение не подлежит исполнению: ok={decision.ok!r}, intent={intent!r}"
        )
    decision = _resolve_decision_filters(decision, df)
    result = dispatch_intent(intent, df, decision.params)
    payload = build_summary_payload(decision, result, profile)
    return ExecutionResult(
        decision=decision,
        result=result,
        summary_payload=payload,
    )


def extract_entities(
    decision: AgentDecision,
    result: pd.DataFrame | None,
) -> list[str]:
    """Сущности результата: товары/контрагенты/товарные группы по интенту."""
    return _extract_entities(decision, result)
