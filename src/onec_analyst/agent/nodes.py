"""Узлы LangGraph-агента: контекст, решение, валидация, починка, уточнение, отказ.

Модуль детерминированный там, где это возможно: уточнение, отказ и обработка
ошибок не вызывают LLM; LLM используется только в ``resolve_context`` (fallback),
``agent_decision`` и ``fix_dsl`` — и каждый вызов проходит через guard лимита.
Ни один узел не импортирует Streamlit (ТЗ 6.4 §5.1).
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import date
from typing import Any

from pydantic import ValidationError

from onec_analyst.agent.prompts import (
    build_agent_messages,
    build_fix_messages,
    build_resolve_messages,
)
from onec_analyst.agent.state import (
    MAX_CLARIFICATIONS,
    AgentState,
    DecisionStatus,
    mark_step,
    state_today,
    status_of,
)
from onec_analyst.agent.tools import (
    ParsedCall,
    ToolCallError,
    build_agent_tools,
    parse_arguments_dsl,
    parse_completion,
    validate_meta_arguments,
)
from onec_analyst.clarification import force_interpretation
from onec_analyst.llm_agent import (
    AgentDecision,
    ClarificationPlan,
    LLMError,
    RuleBasedPlanner,
    _short_error,
)
from onec_analyst.pipeline import drop_empty_filters, validate_decision

#: Интенты, которым нужны два периода (period2 обязателен, ADR 0013 D3).
COMPARE_INTENTS = (
    "compare_top_products",
    "counterparty_dynamics",
    "group_dynamics",
)
#: Интенты с границами ``start_date``/``end_date``.
BOUND_INTENTS = (
    "top_bottom_products",
    "negative_revenue",
    "timeseries",
    "total",
)

#: Местоимения, эллипсисы и ссылки на период, при которых нужен resolve_context.
_CONTEXT_TRIGGERS_RE = re.compile(
    r"\b(они|их|им|ими|ним|ними|них|эти|этих|этим|этими|те же|тот же|та же|то же|"
    r"этот же|эта же|а теперь|то же самое|за тот же период|тогда же|"
    r"в тот же период)\b",
    re.IGNORECASE,
)

_FREQ_OVERRIDE_RE = (
    (re.compile(r"по недел|недельн|weekly", re.IGNORECASE), "W"),
    (re.compile(r"по месяц|помесячн|monthly", re.IGNORECASE), "M"),
    (re.compile(r"по квартал|поквартальн|quarterly", re.IGNORECASE), "Q"),
    (re.compile(r"по год|погодов|yearly", re.IGNORECASE), "Y"),
)

_AVERAGE_RE = re.compile(r"средн\w*\s+(чек|выручк|оборот)", re.IGNORECASE)
_VAT_RE = re.compile(r"\bс\s+ндс\b|revenue_vat", re.IGNORECASE)

_PRODUCT_FILTER_KEYS = ("product_filter", "group_filter")


# --------------------------------------------------------------------------- #
# resolve_context
# --------------------------------------------------------------------------- #
def has_context_triggers(question: str) -> bool:
    """Есть ли в вопросе местоимение/эллипсис, требующий памяти диалога."""
    return bool(_CONTEXT_TRIGGERS_RE.search(question or ""))


def _period_pair(value: Any) -> tuple[str, str] | None:
    """Привести ``period`` из DSL к паре ISO-строк (или ``None``)."""
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return str(value[0]), str(value[1])
    return None


def inherit_from_previous(
    previous_dsl: dict[str, Any] | None,
    result_entities: list[str] | None,
    question: str,
) -> dict[str, Any]:
    """Детерминированно унаследовать период/фильтры/сущности прошлого шага.

    Наследуются только безопасные параметры: даты, фильтры, метрика и
    гранулярность. Интент не наследуется — его выбирает модель по новому
    вопросу; ``period2`` не подставляется (ADR 0013 D3).
    """
    if not previous_dsl:
        return {}
    params = previous_dsl.get("params")
    if not isinstance(params, dict):
        return {}
    inherited: dict[str, Any] = {}
    start, end = params.get("start_date"), params.get("end_date")
    if start and end:
        inherited["start_date"] = str(start)
        inherited["end_date"] = str(end)
    pair1 = _period_pair(params.get("period1"))
    pair2 = _period_pair(params.get("period2"))
    if pair1:
        inherited["period1"] = pair1
    if pair2:
        inherited["period2"] = pair2
        inherited.setdefault("start_date", pair2[0])
        inherited.setdefault("end_date", pair2[1])
    for key in ("metric", "freq", "group_by", "product_filter", "group_filter"):
        value = params.get(key)
        if value is not None:
            inherited[key] = value
    for pattern, freq in _FREQ_OVERRIDE_RE:
        if pattern.search(question or ""):
            inherited["freq"] = freq
            break
    entities = [item for item in (result_entities or []) if str(item).strip()]
    if entities:
        inherited["entities"] = entities
    return inherited


def describe_context(inherited: dict[str, Any]) -> str:
    """Текстовая приписка к вопросу: какие данные восстановлены из памяти.

    Пишется явно, чтобы модель не «додумывала» период: наследование видно в
    промпте, а не происходит за спиной пользователя.
    """
    parts: list[str] = []
    if inherited.get("start_date") and inherited.get("end_date"):
        parts.append(f"период {inherited['start_date']} — {inherited['end_date']}")
    elif inherited.get("period1") and inherited.get("period2"):
        parts.append(f"периоды {inherited['period1']} и {inherited['period2']}")
    for key in (*_PRODUCT_FILTER_KEYS, "metric", "freq", "group_by"):
        if inherited.get(key) is not None:
            parts.append(f"{key}={json.dumps(inherited[key], ensure_ascii=False)}")
    if inherited.get("entities"):
        parts.append("сущности: " + ", ".join(inherited["entities"]))
    if not parts:
        return ""
    return "Контекст предыдущего шага (восстановлен детерминированно): " + "; ".join(parts) + "."


def _needs_llm_resolution(question: str, inherited: dict[str, Any]) -> bool:
    """Нужен ли LLM-fallback: ссылка на период есть, а наследовать нечего."""
    if inherited.get("start_date") or inherited.get("period1"):
        return False
    return bool(re.search(r"тот же период|тогда же|в тот период|это же время", question, re.I))


def _parse_resolved_question(content: str) -> str | None:
    """Достать ``resolved_question`` из JSON-ответа LLM (иначе ``None``)."""
    text = (content or "").strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    if isinstance(payload, dict):
        value = payload.get("resolved_question")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def resolve_context(state: AgentState) -> dict[str, Any]:
    """Восстановить контекст предыдущего шага (ТЗ 6.4 §2.3).

    Без триггеров узел возвращает вопрос как есть (маршрутизация его вообще не
    вызывает). С триггерами — детерминированное наследование; LLM-fallback
    только если правила не смогли восстановить период.
    """
    trace = mark_step(state, "resolve_context")
    question = str(state.get("question") or "")
    entities = list(state.get("result_entities") or [])
    inherited = inherit_from_previous(state.get("previous_dsl"), entities, question)
    note = describe_context(inherited)
    resolved = f"{question}\n{note}" if note else question

    if not note and _needs_llm_resolution(question, inherited):
        client = state.get("client")
        guard = state.get("guard")
        if client is not None and guard is not None and guard.before_call():
            messages = build_resolve_messages(
                question,
                previous_dsl=state.get("previous_dsl"),
                result_entities=entities,
            )
            try:
                result = client.complete(messages, tools=None)
            except LLMError:
                guard.after_failure()
                result = None
            if result is not None:
                guard.after_success()
                parsed = _parse_resolved_question(result.content)
                if parsed:
                    resolved = parsed
                return {
                    "resolved_question": resolved,
                    "inherited": inherited,
                    "trace": trace,
                    "llm_success": True,
                    "llm_calls": int(state.get("llm_calls") or 0) + 1,
                }

    return {"resolved_question": resolved, "inherited": inherited, "trace": trace}


# --------------------------------------------------------------------------- #
# agent_decision
# --------------------------------------------------------------------------- #
def _supports_tools(client: Any) -> bool:
    """Использует ли клиент нативный tool calling (GigaChat)."""
    return bool(getattr(client, "supports_tools", False))


def _clarify_decision(
    *,
    missing: list[str],
    question: str,
    suggestions: list[str] | None = None,
    source: str = "llm",
) -> AgentDecision:
    """Собрать решение-уточнение с каноническими ключами."""
    plan = ClarificationPlan(
        question=question,
        missing=missing,
        suggestions=suggestions or [],
    )
    dsl = plan.model_dump(mode="json", exclude_none=True)
    return AgentDecision(
        ok=True,
        intent="clarification",
        clarification=plan,
        dsl=dsl,
        source=source,
        status="clarify",
    )


def _refuse_decision(reason: str, *, source: str = "llm") -> AgentDecision:
    """Собрать решение-отказ с обязательной причиной."""
    return AgentDecision(
        ok=False,
        intent=None,
        dsl={"intent": "refusal", "reason": reason},
        source=source,
        status="refuse",
        refusal_reason=reason,
    )


def _iso_dates(value: Any) -> list[date]:
    """Все ISO-даты из значения DSL (строка, пара, вложенный список)."""
    found: list[date] = []
    if isinstance(value, str):
        try:
            found.append(date.fromisoformat(value[:10]))
        except ValueError:
            return []
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(_iso_dates(item))
    return found


def _has_future_period(params: dict[str, Any], today: date) -> bool:
    """Период начинается позже сегодняшней даты (данных за будущее ещё нет).

    Конец периода позже ``today`` допустим: «этот месяц» — с 1-го по последний
    день календарного месяца, а за будущие дни данных просто нет (итог считается
    по имеющимся). Отказ — только если **начало** периода уже в будущем
    (например, «сколько заработаем в 2030»).
    """
    for key in ("start_date", "period1", "period2"):
        dates = _iso_dates(params.get(key))
        if dates and dates[0] > today:
            return True
    return False


def _mvp_refusal(
    question: str,
    decision: AgentDecision,
    state: AgentState,
) -> str | None:
    """Причина отказа для неподдерживаемого в MVP запроса (ADR 0013 D4).

    Проверка детерминированная и стоит **после** выбора инструмента: она не
    угадывает интент, а не даёт исполнить заведомо неподдерживаемый расчёт.
    """
    text = question or ""
    lowered = text.lower()
    params = decision.params or {}
    if "products" in params or "revenue_vat" in params or "metrics" in params:
        return "Сравнение произвольного набора товаров и выручка с НДС не поддерживаются."
    for key in _PRODUCT_FILTER_KEYS:
        if isinstance(params.get(key), (list, tuple)):
            return "Несколько значений в фильтре не поддерживаются: передайте одну подстроку."
    if decision.intent == "counterparty_dynamics" and params.get("product_filter"):
        return "Фильтр по товару у динамики контрагентов не поддерживается."
    if _VAT_RE.search(text):
        return "Выручка с НДС (revenue_vat) не поддерживается: доступна выручка без НДС."
    if _AVERAGE_RE.search(text):
        return (
            "Расчёт среднего чека не поддерживается. "
            "Доступны только суммарная выручка и количество."
        )
    entities = [str(item) for item in (state.get("result_entities") or []) if str(item)]
    mentions_entities = any(entity.lower() in lowered for entity in entities)
    if entities and mentions_entities and decision.intent in (
        "compare_top_products",
        "timeseries",
    ):
        if decision.intent == "timeseries":
            return (
                "Динамика по произвольному набору товаров не поддерживается: "
                "выберите один товар или постройте ряд по всем продажам."
            )
        return (
            "Сравнение произвольного набора товаров не поддерживается: "
            "доступно сравнение топов за два периода."
        )
    return None


def _apply_inheritance(decision: AgentDecision, inherited: dict[str, Any]) -> AgentDecision:
    """Дополнить аргументы унаследованными датами/фильтрами (без period2).

    ``resolve_context`` уже написал контекст в тексте вопроса; здесь те же
    данные применяются к аргументам, только если модель их не передала.
    """
    if status_of(decision) is not DecisionStatus.OK or not inherited:
        return decision
    params = dict(decision.params or {})
    intent = decision.intent or ""
    changed = False
    if intent in BOUND_INTENTS:
        for key in ("start_date", "end_date"):
            if key not in params and inherited.get(key):
                params[key] = inherited[key]
                changed = True
    if intent in COMPARE_INTENTS:
        if "period1" not in params and inherited.get("period1"):
            params["period1"] = list(inherited["period1"])
            changed = True
        elif "start_date" not in params and inherited.get("start_date"):
            # У прошлого шага был один период — это база сравнения (period1).
            params["period1"] = [inherited["start_date"], inherited["end_date"]]
            changed = True
    for key in ("metric", "freq", "group_by"):
        if key not in params and inherited.get(key):
            params[key] = inherited[key]
            changed = True
    if not changed:
        return decision
    dsl = dict(decision.dsl or {})
    dsl["params"] = params
    return replace(decision, params=params, dsl=dsl)


def _check_compare_periods(decision: AgentDecision) -> AgentDecision:
    """``period2`` обязателен и не равен ``period1`` (иначе сравнение периода с собой)."""
    if status_of(decision) is not DecisionStatus.OK or decision.intent not in COMPARE_INTENTS:
        return decision
    params = decision.params or {}
    period1 = _period_pair(params.get("period1"))
    period2 = _period_pair(params.get("period2"))
    if not period1 or not period2:
        missing = ["period1"] if not period1 else []
        missing.append("period2")
        return _clarify_decision(
            missing=missing,
            question="За какой второй период сравнить? Например, 2023 и 2024.",
            suggestions=["2023 и 2024"],
        )
    if period1 == period2:
        return _clarify_decision(
            missing=["period2"],
            question="За какой второй период сравнить? Первый и второй периоды совпадают.",
            suggestions=["2023 и 2024"],
        )
    return decision


def _decision_from_call(call: ParsedCall, state: AgentState) -> AgentDecision:
    """Нормализовать вызов инструмента в :class:`AgentDecision`."""
    if call.name == "clarify":
        return _clarify_decision(
            missing=[str(item) for item in call.arguments.get("missing") or []],
            question=str(call.arguments.get("question") or "Уточните запрос."),
        )
    if call.name == "refuse":
        return _refuse_decision(str(call.arguments.get("reason") or "Запрос не поддерживается."))
    params = drop_empty_filters(dict(call.arguments or {}))
    return AgentDecision(
        ok=True,
        intent=call.name,
        params=params,
        raw_response=call.raw,
        dsl={"intent": call.name, "params": params},
        source="llm",
        status="ok",
    )


def _apply_guards(
    question: str,
    decision: AgentDecision,
    state: AgentState,
) -> AgentDecision:
    """Детерминированные ограничения поверх выбора модели: MVP и period2."""
    if status_of(decision) is DecisionStatus.CLARIFY:
        # «Покажи продажи 2025»: период есть, разрез не указан. Слабая модель
        # иногда переспрашивает, хотя безопасный итог за период уже определён.
        # Не подменяем даты/товары — берём только детерминированный total и
        # только при явном числовом годе (иначе «сколько заработали?» остаётся
        # уточнением, как требует ADR 0007).
        if not re.search(r"\b20\d{2}\b", question):
            return decision
        planner = state.get("planner") or RuleBasedPlanner()
        fallback = planner.plan(
            question,
            data_profile=state.get("profile"),
            today=state_today(state),
        )
        if fallback.ok and fallback.intent == "total":
            return replace(fallback, source="rules", status="ok")
        return decision
    if status_of(decision) is not DecisionStatus.OK:
        return decision
    reason = _mvp_refusal(question, decision, state)
    if reason:
        return _refuse_decision(reason, source=decision.source)
    today = state_today(state)
    if _has_future_period(decision.params or {}, today):
        return _refuse_decision(
            "Период позже сегодняшней даты не поддерживается: данных за будущее ещё нет.",
            source=decision.source,
        )
    return _check_compare_periods(decision)


def _render_request(messages: list[dict[str, str]]) -> str:
    """Текст запроса к модели как есть: роли и содержимое, без пересказа.

    Нужен для диагностики неудачного вызова: показать пользователю ровно то, что
    ушло в LLM (системный промпт + вопрос и контекст).
    """
    parts: list[str] = []
    for message in messages:
        role = str(message.get("role") or "unknown")
        content = str(message.get("content") or "")
        parts.append(f"===== {role} =====\n{content}")
    return "\n\n".join(parts)


def agent_decision(state: AgentState) -> dict[str, Any]:
    """Один LLM-вызов с tool calling и нормализация в решение со статусом."""
    trace = mark_step(state, "agent_decision")
    client = state.get("client")
    guard = state.get("guard")
    if client is None:
        return {"decision": None, "error": "LLM-клиент не передан в граф", "trace": trace}
    if guard is not None and not guard.before_call():
        return {
            "decision": None,
            "error": "Лимит вызовов LLM исчерпан",
            "trace": trace,
        }
    question = str(state.get("resolved_question") or state.get("question") or "")
    tools = build_agent_tools() if _supports_tools(client) else None
    messages = build_agent_messages(
        question,
        today=state_today(state),
        previous_dsl=state.get("previous_dsl"),
        result_entities=state.get("result_entities"),
        json_tools=not _supports_tools(client),
    )
    try:
        result = client.complete(messages, tools=tools)
    except LLMError as exc:
        if guard is not None:
            guard.after_failure()
        return {
            "decision": None,
            "error": f"LLM недоступна: {exc}",
            "validation_error": f"LLM недоступна: {exc}",
            "llm_unavailable": True,
            "llm_request": _render_request(messages),
            "trace": trace,
        }
    if guard is not None:
        guard.after_success()
    calls = int(state.get("llm_calls") or 0) + 1
    try:
        call = parse_completion(result)
    except ToolCallError as exc:
        return {
            "decision": None,
            "error": str(exc),
            "validation_error": str(exc),
            "llm_success": True,
            "llm_calls": calls,
            "trace": trace,
        }
    try:
        validate_meta_arguments(call)
    except ToolCallError as exc:
        return {
            "decision": None,
            "error": str(exc),
            "validation_error": str(exc),
            "llm_success": True,
            "llm_calls": calls,
            "trace": trace,
        }
    decision = _apply_inheritance(_decision_from_call(call, state), state.get("inherited") or {})
    decision = _apply_guards(question, decision, state)
    return {
        "decision": decision,
        "validation_error": None,
        "error": None,
        "llm_success": True,
        "llm_calls": calls,
        "trace": trace,
    }


# --------------------------------------------------------------------------- #
# validate_dsl / fix_dsl
# --------------------------------------------------------------------------- #
def validate_dsl(state: AgentState) -> dict[str, Any]:
    """Pydantic-валидация DSL через общий ``pipeline.validate_decision``."""
    trace = mark_step(state, "validate_dsl")
    decision = state.get("decision")
    if status_of(decision) is not DecisionStatus.OK:
        return {"trace": trace}
    assert decision is not None  # для типизации; status_of уже проверил
    dsl = decision.dsl or {"intent": decision.intent, "params": decision.params}
    try:
        normalized = validate_decision(dsl)
    except (ValueError, ValidationError) as exc:
        return {"validation_error": _short_error(exc), "trace": trace}
    normalized = replace(
        normalized,
        status="ok",
        source=decision.source,
        raw_response=decision.raw_response,
    )
    return {"decision": normalized, "validation_error": None, "trace": trace}


def fix_dsl(state: AgentState) -> dict[str, Any]:
    """Один повторный LLM-вызов: интент фиксирован, только JSON аргументов."""
    trace = mark_step(state, "fix_dsl")
    decision = state.get("decision")
    retry = int(state.get("retry_count") or 0) + 1
    if decision is None or not decision.intent:
        return {
            "validation_error": state.get("validation_error") or "Нет интента для исправления",
            "retry_count": retry,
            "trace": trace,
        }
    guard = state.get("guard")
    client = state.get("client")
    if guard is not None and not guard.before_call():
        return {
            "validation_error": "Лимит вызовов LLM исчерпан",
            "error": "Лимит вызовов LLM исчерпан",
            "retry_count": retry,
            "trace": trace,
        }
    messages = build_fix_messages(
        intent=decision.intent,
        validation_error=str(state.get("validation_error") or ""),
        failed_args=dict(decision.params or {}),
        today=state_today(state),
    )
    try:
        result = client.complete(messages, tools=None) if client is not None else None
    except LLMError as exc:
        if guard is not None:
            guard.after_failure()
        return {
            "validation_error": f"LLM недоступна: {exc}",
            "error": f"LLM недоступна: {exc}",
            "retry_count": retry,
            "trace": trace,
        }
    if result is None:
        if guard is not None:
            guard.after_failure()
        return {
            "validation_error": "LLM-клиент не передан в граф",
            "retry_count": retry,
            "trace": trace,
        }
    if guard is not None:
        guard.after_success()
    try:
        arguments = parse_arguments_dsl(result.content, intent=decision.intent)
        normalized = validate_decision({"intent": decision.intent, "params": arguments})
    except (ToolCallError, ValueError, ValidationError) as exc:
        return {
            "validation_error": _short_error(exc),
            "decision": decision,
            "retry_count": retry,
            "llm_success": True,
            "llm_calls": int(state.get("llm_calls") or 0) + 1,
            "trace": trace,
        }
    normalized = replace(
        normalized,
        status="ok",
        source=decision.source,
        raw_response=result.content,
    )
    return {
        "decision": normalized,
        "validation_error": None,
        "retry_count": retry,
        "llm_success": True,
        "llm_calls": int(state.get("llm_calls") or 0) + 1,
        "trace": trace,
    }


# --------------------------------------------------------------------------- #
# clarify / refuse / handle_error
# --------------------------------------------------------------------------- #
def clarify(state: AgentState) -> dict[str, Any]:
    """Детерминированное уточнение; после лимита кругов — force_interpretation."""
    trace = mark_step(state, "clarify")
    rounds = int(state.get("clarification_rounds") or 0)
    if rounds >= MAX_CLARIFICATIONS:
        planner = state.get("planner") or RuleBasedPlanner()
        forced = force_interpretation(
            str(state.get("question") or ""),
            planner=planner if isinstance(planner, RuleBasedPlanner) else None,
            data_profile=state.get("profile"),
        )
        if forced is not None:
            return {
                "decision": replace(forced, status="ok", trace=tuple(trace)),
                "trace": trace,
            }
        return {
            "decision": AgentDecision(
                ok=False,
                status="error",
                error="Не удалось однозначно разобрать вопрос даже после уточнений",
                source="rules",
                trace=tuple(trace),
            ),
            "trace": trace,
        }
    decision = state.get("decision")
    if decision is None or decision.clarification is None:
        decision = _clarify_decision(
            missing=["intent"],
            question=(
                "Уточните, что показать: топ товаров, динамику по месяцам, "
                "сравнение контрагентов, срез по группам или общий итог?"
            ),
            suggestions=[
                "Сколько мы продали за 2024 год?",
                "Какие 5 товаров продаются хуже остальных за 2024 год?",
            ],
        )
    return {"decision": replace(decision, status="clarify", trace=tuple(trace)), "trace": trace}


def refuse(state: AgentState) -> dict[str, Any]:
    """Детерминированный отказ: причина обязательна, расчёт не выполняется."""
    trace = mark_step(state, "refuse")
    decision = state.get("decision")
    reason = (decision.refusal_reason if decision is not None else None) or (
        decision.error if decision is not None else None
    )
    reason = str(reason or "").strip() or (
        "Запрос не относится к поддерживаемой аналитике продаж."
    )
    if decision is None:
        decision = _refuse_decision(reason)
    return {
        "decision": replace(
            decision,
            ok=False,
            status="refuse",
            refusal_reason=reason,
            trace=tuple(trace),
        ),
        "trace": trace,
    }


def handle_error(state: AgentState) -> dict[str, Any]:
    """Техническая ошибка/лимит/сеть: fallback в правила или честная ошибка.

    Лимит и разбор вопроса остаются как были: при исчерпанном лимите и при
    непонятом вопросе приложение отдаёт детерминированный ответ правилами
    (ТЗ 6.4 §4.2). А вот **недоступность самого LLM** (таймаут/сеть) правилами
    не подменяется: пользователь включил LLM, значит ответ должен быть её —
    либо честная ошибка с просьбой выбрать другого провайдера (DECISIONS #20).
    """
    trace = mark_step(state, "handle_error")
    decision = state.get("decision")
    error = (
        state.get("error")
        or state.get("validation_error")
        or (decision.error if decision is not None else None)
        or "Не удалось получить решение"
    )
    if state.get("llm_unavailable"):
        return {
            "decision": AgentDecision(
                ok=False,
                status="error",
                error=str(error),
                llm_request=str(state.get("llm_request") or ""),
                source="llm",
                trace=tuple(trace),
            ),
            "error": error,
            "trace": trace,
        }
    planner = state.get("planner") or RuleBasedPlanner()
    question = str(state.get("resolved_question") or state.get("question") or "")
    try:
        fallback = planner.plan(question, today=state_today(state))
    except (ValueError, TypeError):
        fallback = None
    if fallback is not None and fallback.ok and fallback.intent not in (None, "clarification"):
        return {
            "decision": replace(fallback, status="fallback", trace=tuple(trace)),
            "error": error,
            "trace": trace,
        }
    return {
        "decision": AgentDecision(
            ok=False,
            status="error",
            error=str(error),
            source="rules",
            trace=tuple(trace),
        ),
        "trace": trace,
    }


__all__ = [
    "BOUND_INTENTS",
    "COMPARE_INTENTS",
    "agent_decision",
    "clarify",
    "describe_context",
    "fix_dsl",
    "handle_error",
    "has_context_triggers",
    "inherit_from_previous",
    "refuse",
    "resolve_context",
    "validate_dsl",
]
