"""LLM-слой: вопрос пользователя → строгий JSON-DSL → валидация → dispatch_intent.

Модуль отвечает за «перевод» естественного языка в контракт анализа, но **не
считает числа**: результат работы — валидированный DSL, который приложение
передаёт в :func:`analytics.dispatch_intent`.

Схема ответа LLM (ровно один JSON-объект, без markdown)::

    {"intent": "top_bottom_products",
     "params": {"metric": "revenue", "limit": 5, "ascending": false,
                "start_date": "2024-01-01", "end_date": "2024-12-31"}}

    {"intent": "clarification",
     "question": "За какой период сравнить продажи?",
     "missing": ["period1", "period2"],
     "suggestions": ["2023 и 2024", "январь–июнь 2024 против июля–декабря 2024"]}

Модуль поддерживает три режима:

* ``LLM_PROVIDER=openai`` / ``deepseek`` / ``ollama`` — вызов OpenAI-совместимого
  Chat Completions API (DeepSeek и Ollama поддерживают тот же протокол);
* ``LLM_PROVIDER=none`` (по умолчанию) — детерминированный разбор вопроса по
  правилам (:class:`RuleBasedPlanner`), чтобы приложение работало без ключа;
* невалидный ответ модели — одна попытка «починить» JSON с сообщением об ошибке,
  затем дружелюбная ошибка (приложение показывает список доступных интентов).

Pydantic-модели повторяют контракт :data:`analytics.INTENT_REGISTRY` /
:data:`analytics.INTENT_PARAMETERS`; рассинхронизация проверяется функцией
:func:`check_schema_sync` (нужен pandas-интерпретатор) и тестом
``smoke_llm.py`` (нужен pydantic-интерпретатор).

Запуск самопроверки без UI::

    python llm_agent.py --show-prompt
    python llm_agent.py --export-schema docs/intent_schema.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Final, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
)

from onec_analyst.paths import find_repo_root

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "CHOOSABLE_PROVIDERS",
    "INTENT_DESCRIPTIONS",
    "INTENT_EXAMPLES",
    "INTENT_NAMES",
    "INTENT_PARAMETER_NAMES",
    "SYSTEM_PROMPT",
    "AgentDecision",
    "ClarificationPlan",
    "CompareTopsParams",
    "CompareTopsPlan",
    "CompletionResult",
    "GigaChatClient",
    "IntentDSL",
    "LLMClient",
    "LLMError",
    "LLMPlanner",
    "LLMSettings",
    "NegativeRevenueParams",
    "NegativeRevenuePlan",
    "ProviderCheck",
    "ProviderOption",
    "RuleBasedPlanner",
    "ToolCall",
    "available_providers",
    "build_messages",
    "build_system_prompt",
    "build_tools",
    "check_provider",
    "check_providers",
    "check_schema_sync",
    "export_schema",
    "guard_decision",
    "guard_metric",
    "is_negative_revenue_question",
    "is_quantity_question",
    "make_client",
    "make_planner",
    "ollama_capabilities",
    "parse_plan_json",
    "provider_catalog",
    "settings_from_env",
    "to_dispatch_params",
    "warmup_ollama",
    "with_ollama_capabilities",
]

# --------------------------------------------------------------------------- #
# Контракт интентов (должен совпадать с analytics.INTENT_PARAMETERS)
# --------------------------------------------------------------------------- #
INTENT_NAMES: Final[tuple[str, ...]] = (
    "top_bottom_products",
    "compare_top_products",
    "negative_revenue",
    "timeseries",
    "counterparty_dynamics",
    "group_dynamics",
    "total",
)

INTENT_PARAMETER_NAMES: Final[dict[str, tuple[str, ...]]] = {
    "top_bottom_products": (
        "metric",
        "limit",
        "ascending",
        "start_date",
        "end_date",
        "product_filter",
        "group_filter",
    ),
    "compare_top_products": ("period1", "period2", "top_n", "metric"),
    "negative_revenue": ("start_date", "end_date", "group_by"),
    "timeseries": (
        "freq",
        "metric",
        "agg",
        "start_date",
        "end_date",
        "fill_gaps",
        "product_filter",
    ),
    "counterparty_dynamics": (
        "period1",
        "period2",
        "metric",
        "top_n",
        "min_abs_delta",
        "sort_by",
        "only_drops",
        "min_base_revenue",
    ),
    "group_dynamics": (
        "period1",
        "period2",
        "group_by",
        "metric",
        "top_n",
        "min_abs_delta",
        "sort_by",
        "only_drops",
        "group_filter",
    ),
    "total": ("start_date", "end_date", "product_filter"),
}

INTENT_DESCRIPTIONS: Final[dict[str, str]] = {
    "top_bottom_products": "лучшие/худшие товары за период (топ или антитоп).",
    "compare_top_products": "сравнение топ-товаров двух периодов (L4L): кто вошёл в топ, кто выпал, кто поднялся или опустился.",
    "negative_revenue": "отрицательная выручка (возвраты, сторно): сумма, число строк и количество в разрезе товаров или контрагентов.",
    "timeseries": "динамика продаж по месяцам/кварталам/годам (временной ряд).",
    "counterparty_dynamics": "сравнение контрагентов в двух периодах: кто вырос/упал (only_drops оставляет только падения).",
    "group_dynamics": "сравнение товарных групп в двух периодах: что просело/выросло (only_drops оставляет только падения).",
    "total": "сколько всего продали за период (одна итоговая строка).",
}

INTENT_EXAMPLES: Final[dict[str, str]] = {
    "top_bottom_products": "Какие 5 товаров продаются хуже остальных за 2024 год?",
    "compare_top_products": "Сравни топ-5 товаров за 2023 и 2024 годы",
    "negative_revenue": "Покажи товары с отрицательной выручкой за 2024 год",
    "timeseries": "Как менялись продажи по месяцам в 2023 году?",
    "counterparty_dynamics": "У каких контрагентов упали продажи в 2024 по сравнению с 2023?",
    "group_dynamics": "Какие товарные группы просели в 2024 по сравнению с 2023?",
    "total": "Сколько мы продали за 2024 год?",
}

MetricName = Literal["revenue", "quantity"]
SortBy = Literal["delta", "delta_abs", "period1_value", "period2_value", "name"]
FreqName = Literal["D", "W", "M", "Q", "Y", "ME", "MS", "QE", "QS", "YE", "YS", "A"]


# --------------------------------------------------------------------------- #
# Базовые типы и парсеры дат
# --------------------------------------------------------------------------- #
def _parse_date_value(value: Any) -> date:
    """Разобрать дату из ответа LLM (ISO или ``ДД.ММ.ГГГГ``)."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        for date_format in ("%Y-%m-%d", "%d.%m.%Y"):
            try:
                return datetime.strptime(text, date_format).date()
            except ValueError:
                continue
    raise ValueError(
        f"Дата должна быть в формате ГГГГ-ММ-ДД или ДД.ММ.ГГГГ, получено {value!r}"
    )


DateValue = Annotated[date, BeforeValidator(_parse_date_value)]


def _validate_period_order(pair: tuple[date, date]) -> tuple[date, date]:
    """Проверить, что начало периода не позже конца."""
    start, end = pair
    if start > end:
        raise ValueError(f"Начало периода {start} позже конца {end}")
    return pair


PeriodPair = Annotated[tuple[DateValue, DateValue], AfterValidator(_validate_period_order)]


# --------------------------------------------------------------------------- #
# Pydantic-модели параметров по интентам
# --------------------------------------------------------------------------- #
class _Params(BaseModel):
    """Базовая модель: лишние поля запрещены (LLM не должна «досылать» своё)."""

    model_config = ConfigDict(extra="forbid")


class TopBottomParams(_Params):
    """Параметры интента «топ/антитоп товаров»."""

    metric: MetricName = "revenue"
    limit: int = Field(default=5, ge=1, le=1000)
    ascending: bool = False
    start_date: DateValue | None = None
    end_date: DateValue | None = None
    product_filter: str | None = Field(
        default=None,
        max_length=200,
        description="Подстрока названия товара (регистр не важен)",
    )
    group_filter: str | None = Field(
        default=None,
        max_length=200,
        description="Подстрока названия товарной группы",
    )


class CompareTopsParams(_Params):
    """Параметры интента «сравнение топов» (L4L)."""

    period1: PeriodPair
    period2: PeriodPair
    top_n: int = Field(default=5, ge=1, le=1000)
    metric: MetricName = "revenue"


class NegativeRevenueParams(_Params):
    """Параметры интента «отрицательная выручка (возвраты/сторно)»."""

    start_date: DateValue | None = None
    end_date: DateValue | None = None
    group_by: Literal["product", "counterparty"] = "product"


class TimeseriesParams(_Params):
    """Параметры интента «временной ряд»."""
    freq: FreqName = "M"
    metric: MetricName = "revenue"
    agg: Literal["sum", "mean", "count"] = "sum"
    start_date: DateValue | None = None
    end_date: DateValue | None = None
    fill_gaps: bool = True
    product_filter: str | None = Field(default=None, max_length=200)


class CounterpartyDynamicsParams(_Params):
    """Параметры интента «динамика контрагентов»."""

    period1: PeriodPair
    period2: PeriodPair
    metric: MetricName = "revenue"
    top_n: int | None = Field(default=None, ge=1, le=1000)
    min_abs_delta: float | None = Field(default=None, ge=0)
    sort_by: SortBy = "delta_abs"
    only_drops: bool = Field(
        default=False,
        description="Оставить только контрагентов с падением продаж (delta < 0)",
    )
    min_base_revenue: float | None = Field(
        default=None,
        ge=0,
        description="Оставить только контрагентов с базовой выручкой period1 >= N",
    )


class GroupDynamicsParams(_Params):
    """Параметры интента «срез по товарным группам»."""

    period1: PeriodPair
    period2: PeriodPair
    group_by: str | None = Field(
        default=None,
        description="Колонка группировки: product, counterparty, product_group; None — товарная группа из витрины",
        max_length=64,
    )
    metric: MetricName = "revenue"
    top_n: int | None = Field(default=None, ge=1, le=1000)
    min_abs_delta: float | None = Field(default=None, ge=0)
    sort_by: SortBy = "delta_abs"
    only_drops: bool = False
    group_filter: str | None = Field(default=None, max_length=200)


class TotalParams(_Params):
    """Параметры интента «общая сумма за период»."""

    start_date: DateValue | None = None
    end_date: DateValue | None = None
    product_filter: str | None = Field(default=None, max_length=200)


# --------------------------------------------------------------------------- #
# Pydantic-модели интентов (discriminated union по полю intent)
# --------------------------------------------------------------------------- #
class TopBottomPlan(BaseModel):
    """DSL интента 1: топ/антитоп товаров."""

    model_config = ConfigDict(extra="forbid")
    intent: Literal["top_bottom_products"] = "top_bottom_products"
    params: TopBottomParams = Field(default_factory=TopBottomParams)


class CompareTopsPlan(BaseModel):
    """DSL интента 6: сравнение топ-товаров двух периодов (L4L)."""

    model_config = ConfigDict(extra="forbid")
    intent: Literal["compare_top_products"] = "compare_top_products"
    params: CompareTopsParams


class NegativeRevenuePlan(BaseModel):
    """DSL интента 7: отрицательная выручка (возвраты/сторно)."""

    model_config = ConfigDict(extra="forbid")
    intent: Literal["negative_revenue"] = "negative_revenue"
    params: NegativeRevenueParams = Field(default_factory=NegativeRevenueParams)


class TimeseriesPlan(BaseModel):
    """DSL интента 3: временной ряд."""

    model_config = ConfigDict(extra="forbid")
    intent: Literal["timeseries"] = "timeseries"
    params: TimeseriesParams = Field(default_factory=TimeseriesParams)


class CounterpartyDynamicsPlan(BaseModel):
    """DSL интента 2: динамика контрагентов."""

    model_config = ConfigDict(extra="forbid")
    intent: Literal["counterparty_dynamics"] = "counterparty_dynamics"
    params: CounterpartyDynamicsParams


class GroupDynamicsPlan(BaseModel):
    """DSL интента 4: срез по товарным группам."""

    model_config = ConfigDict(extra="forbid")
    intent: Literal["group_dynamics"] = "group_dynamics"
    params: GroupDynamicsParams


class TotalPlan(BaseModel):
    """DSL интента 5: общая сумма за период."""

    model_config = ConfigDict(extra="forbid")
    intent: Literal["total"] = "total"
    params: TotalParams = Field(default_factory=TotalParams)


class ClarificationPlan(BaseModel):
    """DSL уточняющего вопроса: модель не угадывает, а просит данные."""

    model_config = ConfigDict(extra="forbid")
    intent: Literal["clarification"] = "clarification"
    question: str = Field(min_length=3, max_length=500)
    missing: list[str] = Field(default_factory=list, max_length=10)
    suggestions: list[str] = Field(default_factory=list, max_length=10)


IntentDSL = Annotated[
    TopBottomPlan
    | CompareTopsPlan
    | NegativeRevenuePlan
    | TimeseriesPlan
    | CounterpartyDynamicsPlan
    | GroupDynamicsPlan
    | TotalPlan
    | ClarificationPlan,
    Field(discriminator="intent"),
]

_DSL_ADAPTER: Final[TypeAdapter[Any]] = TypeAdapter(IntentDSL)

#: Параметры-модели по интентам — источник JSON-схемы для tool calling GigaChat.
_PARAMS_MODELS: Final[dict[str, type[_Params]]] = {
    "top_bottom_products": TopBottomParams,
    "compare_top_products": CompareTopsParams,
    "negative_revenue": NegativeRevenueParams,
    "timeseries": TimeseriesParams,
    "counterparty_dynamics": CounterpartyDynamicsParams,
    "group_dynamics": GroupDynamicsParams,
    "total": TotalParams,
}


def build_tools() -> list[dict[str, Any]]:
    """Собрать описание инструментов (функций) для tool calling.

    Каждый интент — одна функция, имя которой равно имени интента, а
    ``parameters`` — JSON-схема параметров из Pydantic-модели. Модель выбирает
    ровно одну функцию (GigaChat поддерживает один вызов за запрос), а её
    аргументы валидируются тем же :data:`_DSL_ADAPTER`, что и JSON-DSL — поэтому
    tool calling не ослабляет контракт.

    Clarification — не инструмент: модель, которой нужны данные, отвечает
    свободным текстом (без вызова функции), и планировщик трактует его как
    уточняющий вопрос.
    """
    tools: list[dict[str, Any]] = []
    for name in INTENT_NAMES:
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": INTENT_DESCRIPTIONS[name],
                    "parameters": _PARAMS_MODELS[name].model_json_schema(),
                },
            }
        )
    return tools


# --------------------------------------------------------------------------- #
# Результат планирования
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AgentDecision:
    """Итог работы LLM-слоя: что вызвать и что показать пользователю.

    Attributes
    ----------
    ok:
        Удалось ли получить валидный DSL.
    intent:
        Имя интента или ``clarification``.
    params:
        Параметры для :func:`analytics.dispatch_intent`.
    clarification:
        Уточняющий вопрос, если модель попросила данные.
    error:
        Дружелюбное описание ошибки (для fallback-сообщения в UI).
    raw_response:
        Сырой ответ LLM (для блока «Как я это посчитал»).
    llm_request:
        Текст, отправленный модели в последнем вызове (system + user): нужен для
        диагностики, когда ответа нет. Аддитивное поле, по умолчанию пусто.
    dsl:
        JSON-безопасное представление DSL (для показа пользователю).
    source:
        ``"llm"`` или ``"rules"`` — чем получен DSL.
    status:
        Явный статус агентного решения: ``ok`` | ``clarify`` | ``refuse`` |
        ``error`` | ``fallback`` (см. ADR 0013, D2). Поле аддитивное: старые
        конструкторы его не передают и получают ``"ok"``; офлайн-правила
        выставляют ``clarify`` для уточняющего вопроса.
    refusal_reason:
        Причина отказа для ``status="refuse"``: объясняет, что именно не
        поддерживается. Для остальных статусов — ``None``.
    trace:
        Шаги графа, выполненные при получении решения (только имена шагов).
    degraded:
        Путь лимита уточнений (ADR 0007): ответ правилами при ``status="ok"``.
    """

    ok: bool
    intent: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    clarification: ClarificationPlan | None = None
    error: str | None = None
    raw_response: str = ""
    llm_request: str = ""
    dsl: dict[str, Any] = field(default_factory=dict)
    source: str = "rules"
    status: str = "ok"
    refusal_reason: str | None = None
    trace: tuple[str, ...] = ()
    degraded: bool = False


@dataclass(frozen=True)
class ToolCall:
    """Вызов инструмента, который вернула модель (tool calling GigaChat)."""

    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class CompletionResult:
    """Нормированный результат вызова LLM-клиента.

    Либо свободный текст (``content`` — уточняющий вопрос или ответ
    классификатора), либо вызовы инструментов (``tool_calls`` — структура
    интент+параметры). Обе ветки валидируются одним Pydantic-контрактом.
    """

    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()


# --------------------------------------------------------------------------- #
# Парсинг и конвертация DSL
# --------------------------------------------------------------------------- #
def _extract_json_object(text: str) -> dict[str, Any]:
    """Достать первый валидный JSON-объект из ответа LLM (в т.ч. из ```json)."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Пустой ответ модели")
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    decoder = json.JSONDecoder()
    for index, symbol in enumerate(cleaned):
        if symbol != "{":
            continue
        try:
            value, _ = decoder.raw_decode(cleaned[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("В ответе модели не найден JSON-объект")


def parse_plan_json(text: str) -> Any:
    """Распарсить и провалидировать JSON-ответ LLM в :data:`IntentDSL`.

    Raises
    ------
    ValueError
        Если JSON не найден/не разбирается.
    pydantic.ValidationError
        Если структура не соответствует контракту.
    """
    payload = _extract_json_object(text)
    return _DSL_ADAPTER.validate_python(payload)


def to_dispatch_params(plan: Any) -> tuple[str, dict[str, Any]]:
    """Превратить валидированный DSL в аргументы :func:`dispatch_intent`.

    Даты остаются объектами ``date`` — ``analytics`` понимает и их, и строки.
    """
    if isinstance(plan, ClarificationPlan):
        raise ValueError("Уточняющий вопрос не содержит параметров для dispatch_intent")
    intent = str(plan.intent)
    params = plan.params.model_dump(exclude_none=True)
    return intent, params


def _decision_from_plan(plan: Any, *, raw: str, source: str) -> AgentDecision:
    """Собрать :class:`AgentDecision` из валидированного DSL."""
    dsl = plan.model_dump(mode="json", exclude_none=True)
    if isinstance(plan, ClarificationPlan):
        return AgentDecision(
            ok=True,
            intent="clarification",
            clarification=plan,
            raw_response=raw,
            dsl=dsl,
            source=source,
            status="clarify",
        )
    intent, params = to_dispatch_params(plan)
    return AgentDecision(
        ok=True,
        intent=intent,
        params=params,
        raw_response=raw,
        dsl=dsl,
        source=source,
    )


# --------------------------------------------------------------------------- #
# Системный промпт
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT: Final[str] = f"""Ты — переводчик вопросов на русском языке в строгий JSON-DSL
для аналитического приложения по продажам из 1С.

ПРАВИЛА
1. Ответ — РОВНО ОДИН JSON-объект, без markdown, без текста до/после.
2. Формат: {{"intent": "<имя>", "params": {{...}}}}.
3. Числа не считаешь и не выдумываешь — только заполняешь параметры.
4. Выручка по умолчанию — БЕЗ НДС (metric="revenue").
5. Даты — "ГГГГ-ММ-ДД" (допускается "ДД.ММ.ГГГГ"), границы включительно.
6. Если для ответа не хватает данных — верни intent="clarification":
   {{"intent":"clarification","question":"<вопрос>","missing":["period1"],"suggestions":["2023 и 2024"]}}
   Не угадывай период сравнения, метрику или группировку, если вопрос допускает
   несколько прочтений.
7. Допустимы только перечисленные ниже интенты и параметры. Лишние поля запрещены.
8. Если пользователь спрашивает про отрицательную выручку, возвраты, сторно,
   убытки или корректировки — ОБЯЗАТЕЛЬНО используй intent="negative_revenue".
   Для таких запросов ЗАПРЕЩЕНО использовать top_bottom_products.
9. Если назван конкретный товар (например, «Крупа Геркулес»), ОБЯЗАТЕЛЬНО
   передай его в product_filter (подстрока названия, без кавычек).
10. Если названа товарная группа/категория (например, «Бытовая техника»),
    ОБЯЗАТЕЛЬНО передай её в group_filter (доступен в top_bottom_products и
    group_dynamics).
11. Если просят отфильтровать по базовой выручке («у кого выручка/база была
    больше 100к»), передай число в min_base_revenue (только counterparty_dynamics).
12. Средний чек, средняя выручка и средний оборот НЕ поддерживаются: верни
    intent="refuse" с причиной «Расчёт среднего чека не поддерживается.
    Доступны только суммарная выручка и количество.» Не подменяй такой запрос
    топом, итогом или динамикой.
13. metric="quantity" — ТОЛЬКО если пользователь явно просит штуки («в штуках»,
    «по количеству», «сколько единиц»). «Самые продаваемые», «лидеры», «топ по
    продажам», «лучшие товары» — это ВСЕГДА metric="revenue" (деньги). Число в
    «топ-10» — это limit, а не количество штук.

ОТНОСИТЕЛЬНЫЕ ПЕРИОДЫ
- Считай их от «Сегодняшней даты» (см. начало промпта), а не от дат витрины.
- «в этом году» — календарный год сегодняшней даты; «прошлый год» — предыдущий
  календарный год; «позапрошлый год» — год, предшествующий прошлому.
- «в этом месяце» — текущий календарный месяц; «в прошлом месяце» — предыдущий.
- Периоды календарные: год — с 01.01 по 31.12, месяц — с 1-го по последний день.

ИНТЕНТЫ И ПАРАМЕТРЫ
{chr(10).join(f"- {name}: {INTENT_DESCRIPTIONS[name]} Параметры: {', '.join(INTENT_PARAMETER_NAMES[name])}." for name in INTENT_NAMES)}

ПАРАМЕТРЫ
- metric: "revenue" | "quantity" (по умолчанию revenue — выручка без НДС).
- limit, top_n: целое 1..1000.
- ascending: true — худшие/наименьшие, false — лучшие/наибольшие.
- freq: "D" | "W" | "M" (месяц) | "Q" | "Y".
- agg: "sum" | "mean" | "count".
- period1, period2: ["ГГГГ-ММ-ДД", "ГГГГ-ММ-ДД"], начало <= конец.
- sort_by: "delta_abs" | "delta" | "period1_value" | "period2_value" | "name".
- min_abs_delta: неотрицательное число.
- only_drops: true — оставить только падения (group_dynamics и counterparty_dynamics).
- group_by: "product" | "counterparty" | "product_group" | null.
  Для negative_revenue — "product" | "counterparty" (разрез отрицательной выручки).
- product_filter: подстрока названия товара (регистр не важен), например «Крупа».
- group_filter: подстрока названия товарной группы, например «Бытовая техника».
- min_base_revenue: неотрицательное число — фильтр «в первом периоде выручка >= N».

ПРИМЕРЫ
Q: Какие 5 товаров продаются хуже остальных за 2024 год?
A: {{"intent":"top_bottom_products","params":{{"metric":"revenue","limit":5,"ascending":true,"start_date":"2024-01-01","end_date":"2024-12-31"}}}}

Q: Сравни топ-5 товаров за 2023 и 2024 годы.
A: {{"intent":"compare_top_products","params":{{"period1":["2023-01-01","2023-12-31"],"period2":["2024-01-01","2024-12-31"],"top_n":5,"metric":"revenue"}}}}

Q: Покажи товары с отрицательной выручкой за 2024 год.
A: {{"intent":"negative_revenue","params":{{"start_date":"2024-01-01","end_date":"2024-12-31","group_by":"product"}}}}

Q: Сколько продали «Крупа Геркулес» за 2024 год?
A: {{"intent":"total","params":{{"start_date":"2024-01-01","end_date":"2024-12-31","product_filter":"Крупа Геркулес"}}}}

Q: Какие топ-5 товаров в группе «Бытовая техника» за 2024?
A: {{"intent":"top_bottom_products","params":{{"metric":"revenue","limit":5,"ascending":false,"start_date":"2024-01-01","end_date":"2024-12-31","group_filter":"Бытовая техника"}}}}

Q: У каких контрагентов выручка в 2023 была больше 100 000 и упала в 2024?
A: {{"intent":"counterparty_dynamics","params":{{"period1":["2023-01-01","2023-12-31"],"period2":["2024-01-01","2024-12-31"],"metric":"revenue","min_base_revenue":100000,"only_drops":true}}}}

Q: Как менялись продажи по месяцам в 2023 году?
A: {{"intent":"timeseries","params":{{"freq":"M","metric":"revenue","start_date":"2023-01-01","end_date":"2023-12-31"}}}}

Q: У каких контрагентов упали продажи в 2024 по сравнению с 2023?
A: {{"intent":"counterparty_dynamics","params":{{"period1":["2023-01-01","2023-12-31"],"period2":["2024-01-01","2024-12-31"],"metric":"revenue","only_drops":true,"top_n":10}}}}

Q: Какие товарные группы просели в 2024 по сравнению с 2023?
A: {{"intent":"group_dynamics","params":{{"period1":["2023-01-01","2023-12-31"],"period2":["2024-01-01","2024-12-31"],"metric":"revenue","only_drops":true}}}}

Q: Сколько мы продали за 2024 год?
A: {{"intent":"total","params":{{"start_date":"2024-01-01","end_date":"2024-12-31"}}}}

Q: Сравни продажи.
A: {{"intent":"clarification","question":"Какие периоды сравнить и по какому показателю?","missing":["period1","period2"],"suggestions":["2023 и 2024","январь–июнь 2024 против июля–декабря 2024"]}}
"""


def _relative_date_examples(today: date) -> str:
    """Few-shot примеры разбора относительных дат (даты считаются от ``today``).

    Примеры генерируются динамически: если захардкодить их, промпт устареет и
    LLM снова начнёт ошибаться на «прошлом/позапрошлом годе». Формат повторяет
    статические примеры из :data:`SYSTEM_PROMPT`.
    """
    previous_year = today.year - 1
    two_years_ago = today.year - 2
    return (
        "ПРИМЕРЫ С ОТНОСИТЕЛЬНЫМИ ДАТАМИ\n"
        "Q: Сколько мы продали в прошлом году?\n"
        "A: "
        f'{{"intent":"total","params":{{"start_date":"{previous_year}-01-01",'
        f'"end_date":"{previous_year}-12-31"}}}}\n\n'
        "Q: Какие топ-5 товаров продавались лучше всего в марте позапрошлого года?\n"
        "A: "
        f'{{"intent":"top_bottom_products","params":{{"metric":"revenue","limit":5,'
        f'"ascending":false,"start_date":"{two_years_ago}-03-01",'
        f'"end_date":"{two_years_ago}-03-31"}}}}\n\n'
        "Q: У каких контрагентов упали продажи в прошлом году по сравнению с позапрошлым?\n"
        "A: "
        f'{{"intent":"counterparty_dynamics","params":{{'
        f'"period1":["{two_years_ago}-01-01","{two_years_ago}-12-31"],'
        f'"period2":["{previous_year}-01-01","{previous_year}-12-31"],'
        f'"metric":"revenue","only_drops":true}}}}\n'
    )


def build_system_prompt(
    data_profile: dict[str, Any] | None = None,
    *,
    today: date | None = None,
) -> str:
    """Собрать системный промпт: текущая дата, правила DSL и профиль данных.

    Parameters
    ----------
    data_profile:
        Профиль витрины (строки, диапазон дат, число товаров/контрагентов);
        добавляется в конец промпта блоком ``ДАННЫЕ``.
    today:
        Дата, которую считаем «сегодняшней». По умолчанию — текущая дата
        компьютера; параметр нужен для воспроизводимых тестов.

    Returns
    -------
    str
        Текст системного промпта с текущей датой в первом абзаце.

    Notes
    -----
    Без текущей даты LLM неверно разбирает относительные периоды («прошлый
    год», «позапрошлый год», «в этом месяце»), поэтому дата передаётся явно.
    Дополнительно к правилам добавляются Few-Shot примеры с датами,
    рассчитанными от этой же даты: они закрепляют арифметику сдвига лет.
    """
    current_date = today or datetime.now().date()
    prompt = (
        f"Сегодняшняя дата: {current_date.strftime('%Y-%m-%d')}.\n"
        "Используй её для расчёта относительных дат (например, 'прошлый год', "
        "'позапрошлый год', 'в этом месяце').\n\n"
        + SYSTEM_PROMPT
        + "\n\n"
        + _relative_date_examples(current_date)
    )
    if data_profile:
        lines = []
        if data_profile.get("rows") is not None:
            lines.append(f"строк в витрине: {data_profile['rows']}")
        if data_profile.get("date_min") and data_profile.get("date_max"):
            lines.append(
                f"доступный период данных: {data_profile['date_min']} … {data_profile['date_max']}"
            )
        if data_profile.get("products") is not None:
            lines.append(f"товаров: {data_profile['products']}")
        if data_profile.get("counterparties") is not None:
            lines.append(f"контрагентов: {data_profile['counterparties']}")
        if data_profile.get("source"):
            lines.append(f"источник: {data_profile['source']}")
        if lines:
            prompt += "\nДАННЫЕ\n" + "\n".join(f"- {line}" for line in lines) + "\n"
    return prompt


def build_messages(
    question: str,
    history: Sequence[dict[str, str]] | None = None,
    data_profile: dict[str, Any] | None = None,
    *,
    today: date | None = None,
) -> list[dict[str, str]]:
    """Собрать сообщения для Chat Completions: система + история + вопрос.

    ``today`` пробрасывается в системный промпт, чтобы LLM корректно считала
    относительные даты; по умолчанию используется текущая дата.
    """
    messages: list[dict[str, str]] = [
        {"role": "system", "content": build_system_prompt(data_profile, today=today)}
    ]
    for message in list(history or [])[-6:]:
        role = message.get("role")
        content = str(message.get("content", "")).strip()
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content[:2000]})
    messages.append({"role": "user", "content": question.strip()})
    return messages


# --------------------------------------------------------------------------- #
# Настройки и HTTP-клиент
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LLMSettings:
    """Настройки LLM из переменных окружения.

    Attributes
    ----------
    provider:
        ``none`` | ``openai`` | ``deepseek`` | ``ollama``.
    model:
        Имя модели; по умолчанию зависит от провайдера.
    api_key:
        Ключ API (для ollama — заглушка ``"ollama"``: сервер ключ не проверяет,
        но заголовок нужен OpenAI-совместимому клиенту).
    base_url:
        Базовый URL OpenAI-совместимого API (без ``/chat/completions``).
    timeout:
        Таймаут запроса в секундах.
    temperature:
        Температура генерации (0 — детерминированно).
    json_mode:
        Запрашивать ли у провайдера ``response_format={"type":"json_object"}``.
    """

    provider: str = "none"
    model: str = ""
    api_key: str = ""
    base_url: str = ""
    timeout: float = 60.0
    temperature: float = 0.0
    json_mode: bool = True
    scope: str = "GIGACHAT_API_PERS"
    auth_url: str = ""
    ca_bundle: str = ""
    verify_tls: bool = True
    #: Контекст Ollama (`num_ctx`); 0 — не задавать (дефолт сервера). Только ollama.
    num_ctx: int = 0
    #: Режим размышлений qwen3 (`think`); False — отключён. Только ollama.
    think: bool = True
    #: Отправлять ли нативные tools. Только ollama: для импортированных моделей
    #: (GGUF) сборка Ollama не умеет tools без ``--jinja``, и запрос падает 500.
    #: ``False`` переключает агента на JSON-адаптер (``{"tool": ..., "arguments": ...}``).
    tools: bool = True
    #: Время удержания модели в памяти Ollama после запроса (``keep_alive``).
    #: Пустая строка — не передавать (действует серверная OLLAMA_KEEP_ALIVE).
    #: Передаётся в корень payload нативного ``/api/chat``; слой ``/v1`` его
    #: молча игнорирует — для него работает только серверная переменная.
    keep_alive: str = ""

    @property
    def is_configured(self) -> bool:
        """Готов ли провайдер к вызову (для ollama ключ не нужен)."""
        if self.provider == "none":
            return False
        if self.provider == "ollama":
            return bool(self.base_url or True)
        return bool(self.api_key)


_DEFAULT_MODELS: Final[dict[str, str]] = {
    "openai": "gpt-4o-mini",
    "deepseek": "deepseek-chat",
    # Локальная модель по умолчанию: CPU-only, 16 ГБ ОЗУ — 4B-квант.
    # Источник: `ollama pull qwen3:4b`; имя — ровно как в Ollama, без алиасов.
    "ollama": "qwen3:4b",
    "gigachat": "GigaChat-2-Pro",
    "mistral": "ministral-14b-2512",
}
_DEFAULT_BASE_URLS: Final[dict[str, str]] = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "ollama": "http://localhost:11434/v1",
    "gigachat": "https://api.giga.chat/v1",
    "mistral": "https://api.mistral.ai/v1",
}
#: Эндпоинты OAuth-токена (для провайдеров с двухшаговой авторизацией).
_DEFAULT_AUTH_URLS: Final[dict[str, str]] = {
    "gigachat": "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
}

#: Настройки, которые принадлежат провайдеру из ``LLM_PROVIDER``: общее имя
#: (``LLM_MODEL``/``LLM_BASE_URL``/``LLM_KEEP_ALIVE``) для них — сокращение для
#: ``<NAME>_<PROVIDER>``. Не-провайдерные (``LLM_TIMEOUT``/``LLM_TEMPERATURE``)
#: применяются ко всем.
_PROVIDER_SCOPED: Final[frozenset[str]] = frozenset(
    {"LLM_MODEL", "LLM_BASE_URL", "LLM_KEEP_ALIVE"}
)


def with_dotenv(overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    """Окружение процесса + ``.env`` + явные переопределения.

    Явные ``overrides`` важнее и окружения, и файла: используется тестами и
    вызовами, где настройку надо задать на один раз. Пустая строка в
    ``overrides`` удаляет значение (переменная считается незаданной).
    """
    values = _environment_with_dotenv()
    for key, value in (overrides or {}).items():
        if value:
            values[key] = value
        else:
            values.pop(key, None)
    return values


def _pick_setting(
    values: Mapping[str, str], setting: str, provider: str, env_provider: str
) -> str:
    """Забрать настройку ``setting`` для провайдера ``provider`` из окружения.

    1. ``<SETTING>_<PROVIDER>`` — если задано, используется всегда (пустая
       строка — тоже заданное значение: «у провайдера своей настройки нет»).
    2. Провайдерные настройки (``_PROVIDER_SCOPED``: модель и базовый URL) из
       общего ``<SETTING>`` берутся только для провайдера по умолчанию
       (``env_provider``). Чужому провайдеру они не достаются: его вызывающий
       подставит собственный дефолт. Это и есть защита от того, что
       ``LLM_MODEL=deepseek-chat`` уйдёт в GigaChat.
    3. Остальные настройки (``LLM_TIMEOUT``/``LLM_TEMPERATURE``/``LLM_JSON_MODE``)
       не привязаны к провайдеру и применяются всем одинаково.
    """
    specific_value = values.get(f"{setting}_{provider.upper()}")
    if specific_value is not None:
        return specific_value.strip()
    generic_value = values.get(setting)
    if setting in _PROVIDER_SCOPED and provider != env_provider:
        return ""
    if generic_value is None:
        return ""
    return generic_value.strip()


_DOTENV_FILE: Final = find_repo_root() / ".env"


def _parse_dotenv(text: str) -> dict[str, str]:
    """Разобрать содержимое ``.env``: строки ``KEY=VALUE``, ``#`` — комментарий.

    Понимает кавычки вокруг значения и префикс ``export`` — чтобы файл можно
    было копировать из документации провайдеров.
    """
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _environment_with_dotenv() -> dict[str, str]:
    """Окружение процесса, дополненное значениями из ``.env`` папки проекта.

    Значения из настоящих переменных окружения важнее: ``.env`` только
    заполняет то, чего в окружении нет.
    """
    values = dict(os.environ)
    try:
        text = _DOTENV_FILE.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return values
    for key, value in _parse_dotenv(text).items():
        values.setdefault(key, value)
    return values


def settings_from_env(
    env: dict[str, str] | None = None,
    *,
    provider: str | None = None,
) -> LLMSettings:
    """Прочитать настройки LLM из окружения (с разумными значениями по умолчанию).

    Parameters
    ----------
    env:
        Словарь переменных; по умолчанию — окружение процесса + ``.env``.
        Переданный словарь используется как есть (совместимо с тестами и
        smoke-скриптами, которые передают ``{}`` для «пустого» окружения).
    provider:
        Явно запрошенный провайдер (выбор в UI). Если задан и отличается от
        ``LLM_PROVIDER``, берутся только его собственные переменные
        ``LLM_API_KEY_<PROVIDER>``/``LLM_MODEL_<PROVIDER>``/``LLM_BASE_URL_<PROVIDER>``:
        общие ``LLM_API_KEY``/``LLM_MODEL``/``LLM_BASE_URL`` принадлежат провайдеру
        по умолчанию и на чужого провайдера не распространяются.

    Для локальной Ollama дополнительно читаются ``OLLAMA_MODEL`` и
    ``OLLAMA_BASE_URL`` (только при ``provider="ollama"``): они уступают
    ``LLM_MODEL_OLLAMA``/``LLM_BASE_URL_OLLAMA`` и существуют для явного
    локального профиля. Дефолт модели — ``qwen3:4b``.
    """
    values = env if env is not None else _environment_with_dotenv()
    env_provider = (values.get("LLM_PROVIDER") or "none").strip().lower()
    provider = (provider or env_provider).strip().lower()
    if provider not in ("none", "openai", "deepseek", "ollama", "gigachat", "mistral"):
        raise ValueError(
            "Неизвестный LLM_PROVIDER="
            f"{provider!r}; допустимо: none, openai, deepseek, ollama, gigachat, mistral"
        )

    def pick(name: str) -> str:
        """Настройка провайдера: ``<NAME>_<PROVIDER>``, а общий ``<NAME>`` — см. ниже."""
        return _pick_setting(values, name, provider, env_provider)

    def ollama_alias(name: str) -> str:
        """Локальные ``OLLAMA_MODEL``/``OLLAMA_BASE_URL`` (только для ollama).

        Добавлены аддитивно к ``LLM_*``-механизму: существующие переменные
        имеют приоритет, а эти читаются, только если провайдер — ``ollama``.
        Имена не пересекаются с переменными самого сервера Ollama
        (``OLLAMA_HOST``/``OLLAMA_MODELS``): ``OLLAMA_MODEL``/``OLLAMA_BASE_URL``
        описывают клиент, а не сервер.
        """
        if provider != "ollama":
            return ""
        return (values.get(name) or "").strip()

    model = (
        pick("LLM_MODEL")
        or ollama_alias("OLLAMA_MODEL")
        or _DEFAULT_MODELS.get(provider, "")
    )
    base_url = (
        pick("LLM_BASE_URL")
        or ollama_alias("OLLAMA_BASE_URL")
        or _DEFAULT_BASE_URLS.get(provider, "")
    )
    json_mode = (pick("LLM_JSON_MODE") or ("0" if provider == "ollama" else "1")) != "0"
    scope = (values.get("GIGACHAT_SCOPE") or "GIGACHAT_API_PERS").strip()
    auth_url = (
        values.get("GIGACHAT_AUTH_URL") or _DEFAULT_AUTH_URLS.get(provider, "")
    ).strip()
    ca_bundle = (values.get("LLM_TLS_CA_BUNDLE") or "").strip()
    verify_tls = (values.get("LLM_TLS_VERIFY") or "1") != "0"
    num_ctx = 0
    think = True
    tools = True
    keep_alive = ""
    if provider == "ollama":
        try:
            num_ctx = int(ollama_alias("OLLAMA_NUM_CTX") or "8192")
        except ValueError as exc:  # pragma: no cover - защита от кривого env
            raise ValueError(f"OLLAMA_NUM_CTX должен быть числом: {exc}") from exc
        think = (ollama_alias("OLLAMA_THINK") or "1").strip().lower() not in (
            "0",
            "false",
            "no",
            "off",
        )
        # Импортированные GGUF-модели в Ollama 0.40.16 не умеют tools без
        # ``--jinja`` (флага нет): OLLAMA_TOOLS=0 переключает агента на JSON-адаптер.
        tools = (ollama_alias("OLLAMA_TOOLS") or "1").strip().lower() not in (
            "0",
            "false",
            "no",
            "off",
        )
        keep_alive = pick("LLM_KEEP_ALIVE")
    # Таймаут — аддитивно провайдерный: ``LLM_TIMEOUT_<PROVIDER>`` важнее общего
    # ``LLM_TIMEOUT``. Читаем напрямую, а не через ``pick``: она возвращает
    # пустую провайдерную строку как «задано», и общий таймаут был бы потерян.
    generic_timeout = (values.get("LLM_TIMEOUT") or "").strip()
    provider_timeout = (values.get(f"LLM_TIMEOUT_{provider.upper()}") or "").strip()
    try:
        timeout = float(provider_timeout or generic_timeout or "60")
        temperature = float(pick("LLM_TEMPERATURE") or "0")
    except ValueError as exc:  # pragma: no cover - защита от кривого env
        raise ValueError(f"LLM_TIMEOUT/LLM_TEMPERATURE должны быть числами: {exc}") from exc
    return LLMSettings(
        provider=provider,
        model=model,
        # Ollama не проверяет ключ, но OpenAI-совместимый клиент отправляет
        # заголовок только при непустом ключе — подставляем заглушку.
        api_key=pick("LLM_API_KEY") or ("ollama" if provider == "ollama" else ""),
        base_url=base_url.rstrip("/"),
        timeout=timeout,
        temperature=temperature,
        json_mode=json_mode,
        scope=scope,
        auth_url=auth_url.rstrip("/"),
        ca_bundle=ca_bundle,
        verify_tls=verify_tls,
        num_ctx=num_ctx,
        think=think,
        tools=tools,
        keep_alive=keep_alive,
    )


#: Провайдеры, которые можно выбрать в UI (``none`` — офлайн-режим, не провайдер).
CHOOSABLE_PROVIDERS: Final[tuple[str, ...]] = (
    "deepseek",
    "gigachat",
    "openai",
    "mistral",
    "ollama",
)

@dataclass(frozen=True)
class ProviderOption:
    """Провайдер в переключателе UI: настройки + признак «настроен».

    ``configured=False`` — провайдер показан в списке, но не готов к вызову
    (нет ключа/сертификата); UI помечает его «не настроен».
    """

    settings: LLMSettings
    configured: bool


def _ssl_trust_ready(settings: LLMSettings) -> bool:
    """Сможет ли TLS-соединение провайдера установиться с текущими настройками.

    Проверка TLS включена всегда, кроме явного dev-флага ``LLM_TLS_VERIFY=0``
    (см. :meth:`GigaChatClient._ssl_context`). Если задан ``LLM_TLS_CA_BUNDLE``,
    файл должен существовать — иначе соединение упадёт при первом же запросе,
    а не при чтении настроек.
    """
    if not settings.verify_tls:
        return True
    if not settings.ca_bundle:
        return True
    return Path(settings.ca_bundle).is_file()


#: Значения ``OLLAMA_ENABLED``, которыми владелец включает локальную модель.
#: Флаг живёт только в проверке доступности: это не клиентская настройка,
#: поэтому в :class:`LLMSettings` его нет.
_OLLAMA_ENABLED_VALUES: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})


def _is_provider_configured(
    values: dict[str, str], name: str, settings: LLMSettings
) -> bool:
    """Доступен ли провайдер по конфигурации (без сетевых проверок).

    Облачные провайдеры — есть ключ (общий для ``LLM_PROVIDER`` или собственный
    ``LLM_API_KEY_<PROVIDER>``). GigaChat дополнительно требует готовности TLS:
    без корневого сертификата НУЦ Минцифры (или указания несуществующего пути
    в ``LLM_TLS_CA_BUNDLE``) OAuth-запрос падает с ``CERTIFICATE_VERIFY_FAILED``.

    Ollama — локальная, ключ ей не нужен, но и доступной по умолчанию она не
    считается: сервер может быть не запущен, а модель не скачана. Поэтому она
    включается **только** явным флагом ``OLLAMA_ENABLED`` (``1``/``true``/
    ``yes``/``on``, регистр не важен) — opt-in, default ``false``. Проверки связи
    здесь нет: недоступный сервер даст понятную ошибку при реальном вызове.
    """
    default = (values.get("LLM_PROVIDER") or "none").strip().lower()
    if name == "ollama":
        enabled = (values.get("OLLAMA_ENABLED") or "").strip().lower()
        return enabled in _OLLAMA_ENABLED_VALUES and settings.is_configured
    dedicated = (values.get(f"LLM_API_KEY_{name.upper()}") or "").strip()
    if not (name == default or dedicated) or not settings.is_configured:
        return False
    return _ssl_trust_ready(settings)


def provider_catalog(env: dict[str, str] | None = None) -> list[ProviderOption]:
    """Все выбираемые провайдеры (включая ненастроенные) для переключателя UI.

    Отличие от :func:`available_providers`: возвращает запись на каждого
    провайдера из :data:`CHOOSABLE_PROVIDERS`, даже без ключа — чтобы UI показал
    «не настроен». Провайдер по умолчанию идёт первым.
    """
    values = env if env is not None else _environment_with_dotenv()
    default = (values.get("LLM_PROVIDER") or "none").strip().lower()
    options: list[ProviderOption] = []
    for name in CHOOSABLE_PROVIDERS:
        try:
            settings = settings_from_env(values, provider=name)
        except ValueError:
            continue
        options.append(
            ProviderOption(
                settings=settings,
                configured=_is_provider_configured(values, name, settings),
            )
        )
    options.sort(key=lambda item: 0 if item.settings.provider == default else 1)
    return options


def available_providers(env: dict[str, str] | None = None) -> list[LLMSettings]:
    """Настроенные провайдеры (только готовые к вызову), по умолчанию — первым."""
    return [option.settings for option in provider_catalog(env) if option.configured]


class LLMError(RuntimeError):
    """Ошибка вызова LLM (сеть, HTTP-статус, пустой ответ)."""


#: HTTP-статусы, при которых допустим JSON-fallback неподдержки tools.
_TOOLS_UNSUPPORTED_STATUSES: Final[frozenset[str]] = frozenset({"400", "404", "422"})

#: Явные маркеры неподдержки tools в тексте ошибки провайдера (ТЗ 6.4 §4.2).
#: Совпадение регистронезависимое; маркеры полные, а не по одному слову, чтобы
#: «unknown function»/«invalid model»/ошибка схемы не уводили в fallback.
_TOOLS_UNSUPPORTED_MARKERS: Final[tuple[str, ...]] = (
    "tools are not supported",
    "tool calling is not supported",
    "function calling is not supported",
    "does not support tools",
    "does not support function",
    "unknown field 'tools'",
    'unknown field "tools"',
    "unrecognized request argument: tools",
    "unrecognized request argument supplied: tools",
)


def _tools_unsupported(error: LLMError) -> bool:
    """Похоже ли, что провайдер не поддерживает tools (а не сломан запрос).

    Fallback на JSON разрешён только при одновременном выполнении двух условий
    (ТЗ 6.4 §4.2):

    1. HTTP-статус 400/404/422 — 500, сетевая ошибка и таймаут исключены;
    2. текст ошибки содержит один из :data:`_TOOLS_UNSUPPORTED_MARKERS` — 400
       «invalid model» и 422 про схему («Type properties…»), где слова
       «tool»/«function» встречаются случайно, сюда не попадают.
    """
    text = str(error)
    status_match = re.search(r"HTTP (\d{3})", text)
    if status_match is None or status_match.group(1) not in _TOOLS_UNSUPPORTED_STATUSES:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in _TOOLS_UNSUPPORTED_MARKERS)


class LLMClient:
    """Минимальный OpenAI-совместимый клиент на стандартной библиотеке.

    Работает с OpenAI, DeepSeek и Ollama (``/v1``) — без внешних зависимостей.
    Если провайдер поддерживает tools, они передаются в запросе; ответ с
    ``tool_calls`` нормализуется в :class:`CompletionResult`. Провайдеры без
    tools (или модель без tool calling) автоматически откатываются к
    текстовому JSON-режиму.
    """

    supports_tools: bool = True

    def __init__(self, settings: LLMSettings) -> None:
        self._settings = settings

    def complete(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
    ) -> CompletionResult:
        """Вызов модели: с tools — нативный tool calling, без tools — текст."""
        if not tools:
            return CompletionResult(content=self.chat(messages))
        try:
            data = self._post_json(self._payload(messages, tools=tools))
        except LLMError as exc:
            if _tools_unsupported(exc):
                # Провайдер/модель не умеет tools: JSON-адаптер — равноправный путь.
                return CompletionResult(content=self.chat(messages))
            raise
        return self._completion_from_response(data)

    def chat(self, messages: list[dict[str, str]]) -> str:
        """Отправить сообщения и вернуть текст ответа модели.

        Raises
        ------
        LLMError
            Сеть недоступна, HTTP-ошибка, пустой ответ или провайдер не настроен.
        """
        settings = self._settings
        payload = self._payload(messages)
        if settings.json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            data = self._post_json(payload)
        except LLMError as exc:
            # Некоторые провайдеры не поддерживают response_format — повторяем без него.
            if settings.json_mode and "response_format" in str(exc):
                payload.pop("response_format", None)
                data = self._post_json(payload)
            else:
                raise
        return self._content_from_response(data)

    def _payload(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Собрать тело Chat Completions; с tools — без response_format."""
        payload: dict[str, Any] = {
            "model": self._settings.model,
            "messages": messages,
            "temperature": self._settings.temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        return payload

    def _headers(self) -> dict[str, str]:
        """Заголовки запроса (ключ не попадает в сообщения об ошибках)."""
        headers = {"Content-Type": "application/json"}
        if self._settings.api_key:
            headers["Authorization"] = f"Bearer {self._settings.api_key}"
        return headers

    def _chat_url(self) -> str:
        """URL чата; Ollama переопределяет на нативный ``/api/chat``."""
        return f"{self._settings.base_url}/chat/completions"

    def _post_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST к Chat Completions и разбор JSON-ответа провайдера."""
        settings = self._settings
        if not settings.is_configured or not settings.base_url:
            raise LLMError("LLM не настроена: задайте LLM_PROVIDER/LLM_API_KEY/LLM_BASE_URL")
        url = self._chat_url()
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=settings.timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")[:500]
            raise LLMError(f"HTTP {exc.code}: {details}") from exc
        except TimeoutError as exc:
            # Таймаут — не «сеть недоступна»: пользователю важно знать лимит
            # времени, который он сам задал (LLM_TIMEOUT / LLM_TIMEOUT_<PROVIDER>).
            raise LLMError(
                f"провайдер не ответил за {settings.timeout:g} с"
            ) from exc
        except urllib.error.URLError as exc:
            # urlopen иногда заворачивает таймаут в URLError(reason=timeout):
            # текст для пользователя должен остаться тем же.
            if isinstance(exc.reason, TimeoutError):
                raise LLMError(
                    f"провайдер не ответил за {settings.timeout:g} с"
                ) from exc
            raise LLMError(f"Сеть недоступна: {exc}") from exc
        except OSError as exc:
            raise LLMError(f"Сеть недоступна: {exc}") from exc
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise LLMError(f"Провайдер вернул не JSON: {body[:200]!r}") from exc
        if not isinstance(data, dict):
            raise LLMError(f"Неожиданный ответ провайдера: {body[:300]!r}")
        return data

    def _content_from_response(self, data: dict[str, Any]) -> str:
        """Достать текст ответа; пустой content — ошибка."""
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"Неожиданный ответ провайдера: {data!r}"[:300]) from exc
        if not isinstance(content, str) or not content.strip():
            raise LLMError("Провайдер вернул пустой ответ")
        return content

    def _completion_from_response(self, data: dict[str, Any]) -> CompletionResult:
        """Разобрать ответ с tool_calls (или текст, если tools не вызваны)."""
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"Неожиданный ответ провайдера: {data!r}"[:300]) from exc
        content = message.get("content")
        text = content if isinstance(content, str) else ""
        calls: list[ToolCall] = []
        for item in message.get("tool_calls") or []:
            function = item.get("function") or {}
            name = str(function.get("name") or "")
            arguments_raw = function.get("arguments") or "{}"
            if isinstance(arguments_raw, str):
                try:
                    arguments = json.loads(arguments_raw)
                except json.JSONDecodeError as exc:
                    raise LLMError(
                        f"Аргументы инструмента {name!r} не JSON: {arguments_raw[:200]!r}"
                    ) from exc
            else:
                arguments = arguments_raw
            if not name:
                raise LLMError("Провайдер вернул tool call без имени инструмента")
            if not isinstance(arguments, dict):
                raise LLMError(
                    f"Аргументы инструмента {name!r} должны быть объектом, "
                    f"получено {type(arguments)!r}"
                )
            calls.append(ToolCall(name=name, arguments=arguments))
        if not text.strip() and not calls:
            raise LLMError("Провайдер вернул пустой ответ")
        return CompletionResult(content=text, tool_calls=tuple(calls))

    def list_models(self) -> list[str]:
        """ID моделей по ``GET {base_url}/models`` (проверка ключа и ``base_url``).

        Подходит DeepSeek/OpenAI/ollama: единственный дешёвый запрос, который
        подтверждает, что ключ и адрес рабочие, не отправляя сообщений модели.
        """
        url = f"{self._settings.base_url}/models"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._settings.api_key}",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._settings.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")[:500]
            raise LLMError(f"GET /models HTTP {exc.code}: {details}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LLMError(f"Сеть недоступна (GET /models): {exc}") from exc
        try:
            data = json.loads(raw)
            items = data["data"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise LLMError(f"Неожиданный ответ /models: {raw[:200]!r}") from exc
        models: list[str] = []
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict):
                identifier = item.get("id")
                if isinstance(identifier, str) and identifier:
                    models.append(identifier)
        return models


class OllamaChatClient(LLMClient):
    """Нативный ``/api/chat`` Ollama: ``options.num_ctx`` и ``think``.

    OpenAI-совместимый ``/v1`` эти поля игнорирует (CONTEXT остаётся 4096),
    поэтому нативный API — единственный способ применить решения DECISIONS #17.
    Ответ ``/api/chat`` — ``{"message": {"content", "tool_calls"}}`` (без ``choices``),
    поэтому разбор переопределён, а интерфейс (``chat``/``complete``/``list_models``)
    остаётся тем же — вызывающий код не меняется.
    """

    #: Нативный API Ollama поддерживает tools (qwen3:4b — да).
    supports_tools = True

    def __init__(self, settings: LLMSettings) -> None:
        super().__init__(settings)
        #: Токены последнего вызова (``prompt_eval_count``/``eval_count``).
        self.last_usage: dict[str, Any] = {}
        # Импортированные GGUF-модели в Ollama без ``--jinja`` не умеют tools:
        # тогда работаем JSON-адаптером, а не роняем запрос 500 (DECISIONS #23).
        self.supports_tools = settings.tools

    def _post_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST ``/api/chat`` и запоминание токенов ответа (для smoke/диагностики)."""
        data = super()._post_json(payload)
        message = data.get("message") or {}
        self.last_usage = {
            "prompt_eval_count": data.get("prompt_eval_count"),
            "eval_count": data.get("eval_count"),
            "thinking_chars": len(message.get("thinking") or ""),
        }
        return data

    def _api_root(self) -> str:
        """``http://host:11434/v1`` → ``http://host:11434``."""
        base = self._settings.base_url.rstrip("/")
        return base[:-3] if base.endswith("/v1") else base

    def _chat_url(self) -> str:
        return self._api_root() + "/api/chat"

    def _payload(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._settings.model,
            "messages": messages,
            "stream": False,
            "think": self._settings.think,
        }
        # ``temperature`` всегда в явном виде: иначе /api/chat берёт дефолт
        # модели (у gemma3 — 1.0), и LLM_TEMPERATURE=0 молча игнорируется.
        options: dict[str, Any] = {"temperature": self._settings.temperature}
        if self._settings.num_ctx:
            options["num_ctx"] = self._settings.num_ctx
        payload["options"] = options
        if tools:
            payload["tools"] = tools
        # ``keep_alive`` — только в корень payload нативного ``/api/chat``:
        # OpenAI-совместимый слой ``/v1/chat/completions`` его не принимает и
        # молча игнорирует, поэтому для ``/v1`` работает только серверная
        # переменная OLLAMA_KEEP_ALIVE (см. docs/RUNBOOK.md).
        if self._settings.keep_alive:
            payload["keep_alive"] = self._settings.keep_alive
        return payload

    def _message(self, data: dict[str, Any]) -> dict[str, Any]:
        message = data.get("message")
        if not isinstance(message, dict):
            raise LLMError(f"Неожиданный ответ /api/chat: {data!r}"[:300])
        return message

    def _content_from_response(self, data: dict[str, Any]) -> str:
        content = self._message(data).get("content")
        if not isinstance(content, str) or not content.strip():
            raise LLMError("Ollama вернула пустой ответ")
        return content

    def _completion_from_response(self, data: dict[str, Any]) -> CompletionResult:
        message = self._message(data)
        content = message.get("content") or ""
        if not isinstance(content, str):
            content = ""
        calls: list[ToolCall] = []
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            if not isinstance(function, dict):
                continue
            name = function.get("name")
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError as exc:
                    raise LLMError(f"Аргументы инструмента {name!r} — не JSON") from exc
            if (
                not isinstance(name, str)
                or not name.strip()
                or not isinstance(arguments, dict)
            ):
                raise LLMError(f"Некорректный tool_call: {call!r}"[:300])
            calls.append(ToolCall(name=name, arguments=arguments))
        if not content.strip() and not calls:
            raise LLMError("Ollama вернула пустой ответ")
        return CompletionResult(content=content, tool_calls=tuple(calls))


class GigaChatClient:
    """Клиент GigaChat: OAuth-авторизация + tool calling для строгого DSL.

    **LEGACY для health-check/списка моделей.** Граф агента использует
    :class:`onec_analyst.agent.llm.LangChainGigaChatClient` (официальная
    ``langchain-gigachat``, ADR 0014). Этот самописный HTTP-клиент оставлен
    без изменений ради ``check_provider``/``chat_models`` и UI: удалять его
    в этапе 2.5 не требуется.

    Отличается от OpenAI-совместимых провайдеров:

    * **двухшаговая авторизация** (``client_credentials``): обмен authorization
      key (``Authorization: Basic base64(client_id:client_secret)`` + заголовок
      ``RqUID`` + ``scope``) на access token (живёт ~30 минут), затем
      ``Authorization: Bearer <token>`` к ``chat/completions``;
    * **TLS** — сертификаты самоподписанные: доверие к корню НУЦ Минцифры через
      ``ca_bundle`` либо явное ``verify_tls=False`` (только dev-флаг);
    * **tool calling** вместо ``response_format=json_object``: модель выбирает
      одну функцию-интент и возвращает её аргументы, которые затем валидируются
      тем же Pydantic-контрактом, что и JSON-DSL.

    Секреты сюда не попадают: authorization key читается из :class:`LLMSettings`,
    в сообщения об ошибках значения заголовков не включаются.
    """

    supports_tools: bool = True

    #: Запас до истечения токена (секунд), чтобы не использовать просроченный.
    _TOKEN_SKEW_SECONDS: float = 60.0

    def __init__(self, settings: LLMSettings) -> None:
        if settings.provider != "gigachat":
            raise ValueError(
                f"GigaChatClient ожидает provider='gigachat', получено {settings.provider!r}"
            )
        self._settings = settings
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    def _ssl_context(self) -> ssl.SSLContext:
        """TLS-контекст: корень НУЦ Минцифры или явное отключение проверки."""
        if not self._settings.verify_tls:
            return ssl._create_unverified_context()  # явный dev-флаг
        context = ssl.create_default_context()
        if self._settings.ca_bundle:
            context.load_verify_locations(cafile=self._settings.ca_bundle)
        return context

    def _access_token(self) -> str:
        """Вернуть access token (с кэшем) или запросить новый через OAuth."""
        now = time.time()
        if self._token and now < self._token_expires_at - self._TOKEN_SKEW_SECONDS:
            return self._token
        auth_url = self._settings.auth_url or _DEFAULT_AUTH_URLS.get("gigachat", "")
        if not auth_url:
            raise LLMError("GigaChat не настроена: задайте GIGACHAT_AUTH_URL")
        body = urllib.parse.urlencode({"scope": self._settings.scope}).encode("utf-8")
        headers = {
            "Authorization": f"Basic {self._settings.api_key}",
            "RqUID": str(uuid.uuid4()),
            "Content-Type": "application/x-www-form-urlencoded",
        }
        request = urllib.request.Request(auth_url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(
                request, timeout=self._settings.timeout, context=self._ssl_context()
            ) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")[:500]
            raise LLMError(f"OAuth HTTP {exc.code}: {details}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LLMError(f"Сеть недоступна (OAuth): {exc}") from exc
        try:
            data = json.loads(raw)
            token = data["access_token"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise LLMError(f"Неожиданный ответ OAuth: {raw[:200]!r}") from exc
        if "expires_at" in data:
            self._token_expires_at = float(data["expires_at"])
        else:
            self._token_expires_at = now + int(data.get("expires_in", 1800))
        self._token = token
        return token

    def _post(self, url: str, payload: dict[str, Any]) -> str:
        """POST к chat/completions с Bearer-токеном; при 401 — повтор с новым токеном."""

        def _send(token: str) -> str:
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            }
            request = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(
                    request, timeout=self._settings.timeout, context=self._ssl_context()
                ) as response:
                    return response.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as exc:
                details = exc.read().decode("utf-8", errors="replace")[:500]
                raise LLMError(f"HTTP {exc.code}: {details}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise LLMError(f"Сеть недоступна: {exc}") from exc

        try:
            return _send(self._access_token())
        except LLMError as exc:
            had_token = self._token is not None
            if had_token and ("401" in str(exc) or "403" in str(exc)):
                self._token = None
                self._token_expires_at = 0.0
                return _send(self._access_token())
            raise

    def complete(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
    ) -> CompletionResult:
        """Вызвать chat/completions (при ``tools`` — в режиме tool calling)."""
        settings = self._settings
        payload: dict[str, Any] = {
            "model": settings.model,
            "messages": messages,
            "temperature": settings.temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        url = f"{settings.base_url}/chat/completions"
        raw = self._post(url, payload)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMError(f"Провайдер вернул не JSON: {raw[:200]!r}") from exc
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"Неожиданный ответ провайдера: {raw[:300]!r}") from exc
        content = message.get("content")
        if not isinstance(content, str):
            content = ""
        tool_calls: list[ToolCall] = []
        for item in message.get("tool_calls") or []:
            function = item.get("function") or {}
            name = str(function.get("name") or "")
            arguments_raw = function.get("arguments") or "{}"
            if isinstance(arguments_raw, str):
                try:
                    arguments = json.loads(arguments_raw)
                except json.JSONDecodeError:
                    arguments = {}
            else:
                arguments = arguments_raw
            if not isinstance(arguments, dict):
                arguments = {}
            if name:
                tool_calls.append(ToolCall(name=name, arguments=arguments))
        return CompletionResult(content=content, tool_calls=tuple(tool_calls))

    def chat(self, messages: list[dict[str, str]]) -> str:
        """Обычный текстовый вызов без инструментов (для классификатора)."""
        return self.complete(messages, tools=None).content

    def _models_payload(self) -> list[dict[str, Any]]:
        """Сырые записи ``GET /models``: ``[{"id": ..., "type": ...}, ...]``.

        Отдельный шаг нужен, чтобы отличать чат-модели от эмбеддингов: GigaChat
        отдаёт в одном списке и те, и другие, а поле ``type`` — единственный
        признак, по которому их можно разделить надёжно.

        Raises
        ------
        LLMError
            Сеть, HTTP-статус или неожиданный формат ответа.
        """
        url = f"{self._settings.base_url}/models"
        token = self._access_token()
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self._settings.timeout, context=self._ssl_context()
            ) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")[:500]
            raise LLMError(f"GET /models HTTP {exc.code}: {details}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LLMError(f"Сеть недоступна (GET /models): {exc}") from exc
        try:
            data = json.loads(raw)
            items = data["data"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise LLMError(f"Неожиданный ответ /models: {raw[:200]!r}") from exc
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    def models(self) -> list[str]:
        """Список ID моделей, доступных текущему ключу (``GET /models``).

        Лёгкая проверка связки «ключ + сертификат + scope» без генерации
        токенов: OAuth-токен, затем ``GET {base_url}/models``. Ответ GigaChat —
        ``{"object": "list", "data": [{"id": "GigaChat-2-Pro", ...}, ...]}``.

        Возвращает всё, что отдал провайдер, включая эмбеддинги: для
        диагностики (``--check``) нужен полный список. Для выбора модели чата
        в UI есть :meth:`chat_models`.

        Raises
        ------
        LLMError
            Сеть, HTTP-статус или неожиданный формат ответа.
        """
        models: list[str] = []
        for item in self._models_payload():
            identifier = item.get("id")
            if isinstance(identifier, str) and identifier:
                models.append(identifier)
        return models

    def chat_models(self) -> list[str]:
        """Только модели генерации из ``GET /models`` (``type == "chat"``).

        Эмбеддинги и прочие типы (``type`` = ``embedder``/``aicheck``) в
        выпадающий список моделей чата попадать не должны. Если провайдер не
        указал ``type``, модель считается чат-моделью: отбрасывать её по
        отсутствию поля было бы хуже, чем показать лишнюю.
        """
        models: list[str] = []
        for item in self._models_payload():
            kind = item.get("type")
            if isinstance(kind, str) and kind and kind != "chat":
                continue
            identifier = item.get("id")
            if isinstance(identifier, str) and identifier:
                models.append(identifier)
        return models


def make_client(settings: LLMSettings) -> LLMClient | GigaChatClient:
    """Создать HTTP-клиент под провайдера (GigaChat — OAuth + tool calling)."""
    if settings.provider == "gigachat":
        return GigaChatClient(settings)
    if settings.provider == "ollama":
        return OllamaChatClient(settings)
    return LLMClient(settings)


def ollama_capabilities(
    settings: LLMSettings, model: str | None = None
) -> frozenset[str]:
    """Возможности модели Ollama по ``POST /api/show``.

    Набор из поля ``capabilities``: ``completion``, ``tools``, ``thinking``,
    ``vision``… Именно эти флаги решают, можно ли отправлять модели ``think``
    и ``tools``: gemma3, например, отвечает 400 на оба.

    Для не-ollama провайдера возвращается пустой набор. Ошибка сети/HTTP —
    :class:`LLMError`: вызывающий сам решает, что делать (UI оставляет
    настройки как есть и не гадает).
    """
    if settings.provider != "ollama":
        return frozenset()
    name = (model or settings.model).strip()
    if not name:
        return frozenset()
    base = settings.base_url.rstrip("/")
    root = base[:-3] if base.endswith("/v1") else base
    request = urllib.request.Request(
        root + "/api/show",
        data=json.dumps({"model": name}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request, timeout=min(5.0, settings.timeout)
        ) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        TimeoutError,
        OSError,
        json.JSONDecodeError,
    ) as exc:
        raise LLMError(f"Ollama /api/show ({name}) недоступен: {exc}") from exc
    capabilities = data.get("capabilities") if isinstance(data, dict) else None
    if not isinstance(capabilities, list):
        return frozenset()
    return frozenset(item for item in capabilities if isinstance(item, str))


def with_ollama_capabilities(
    settings: LLMSettings, capabilities: Iterable[str] | None
) -> LLMSettings:
    """Выключить ``think``/``tools``, которых у модели нет.

    Настройки могут только **выключать** несовместимую возможность: явный
    ``OLLAMA_THINK=1`` не заставит модель без capability думать (иначе снова
    400 «does not support thinking»). Пустой набор (сервер не ответил) —
    настройки без изменений, чтобы сбой пробы не ломал рабочий профиль.
    """
    if settings.provider != "ollama":
        return settings
    caps = frozenset(str(item) for item in (capabilities or ()))
    if not caps:
        return settings
    think = settings.think and "thinking" in caps
    tools = settings.tools and "tools" in caps
    if think == settings.think and tools == settings.tools:
        return settings
    return replace(settings, think=think, tools=tools)


def warmup_ollama(settings: LLMSettings) -> bool:
    """Предзагрузить модель Ollama в память (``/api/generate`` без ``prompt``).

    Прогрев экономит первый «холодный» запрос: веса грузятся с диска до того,
    как пользователь задал вопрос. Возвращает ``True`` при успехе и ``False``
    при любой ошибке (сервер недоступен, неверная модель, таймаут) — **никогда
    не бросает исключение наружу**: приложение продолжает работу без прогрева.

    Таймаут щедрый (120 с): первая загрузка с SSD на CPU долгая.
    """
    if settings.provider != "ollama":
        return False
    root = settings.base_url.rstrip("/")
    root = root[:-3] if root.endswith("/v1") else root
    payload = {
        "model": settings.model,
        "keep_alive": settings.keep_alive or "30m",
    }
    started = time.monotonic()
    try:
        request = urllib.request.Request(
            root + "/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            response.read()
    except Exception as exc:  # сеть/HTTP/таймаут — прогрев необязателен
        duration_ms = int((time.monotonic() - started) * 1000)
        _LOGGER.warning("warmup_ollama(%s) failed: %s (%d ms)", settings.model, exc, duration_ms)
        return False
    duration_ms = int((time.monotonic() - started) * 1000)
    _LOGGER.info("warmup_ollama(%s) ok (%d ms)", settings.model, duration_ms)
    return True


# --------------------------------------------------------------------------- #
# Планировщики: LLM и детерминированный fallback
# --------------------------------------------------------------------------- #
class LLMPlanner:
    """Планировщик на LLM: вопрос → сообщения → JSON-DSL (или tool call) → Pydantic.

    Для OpenAI-совместимых провайдеров модель отвечает текстом (JSON-DSL),
    который разбирается :func:`parse_plan_json`. Для GigaChat используется tool
    calling: модель выбирает функцию-интент, а её аргументы валидируются тем же
    контрактом. Уточняющий вопрос — свободный текст без вызова функции.
    """

    def __init__(
        self,
        settings: LLMSettings,
        client: LLMClient | GigaChatClient | None = None,
    ) -> None:
        self._settings = settings
        self._client = client or make_client(settings)

    @property
    def supports_tools(self) -> bool:
        """Использует ли клиент tool calling (GigaChat)."""
        return bool(getattr(self._client, "supports_tools", False))

    def _complete(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None,
    ) -> CompletionResult:
        """Вызвать клиент; старые fake-клиенты с ``chat()`` тоже поддержаны."""
        if hasattr(self._client, "complete"):
            return self._client.complete(messages, tools=tools)
        return CompletionResult(content=self._client.chat(messages))

    @staticmethod
    def _to_dsl(
        result: CompletionResult,
        *,
        allow_text_clarification: bool = False,
    ) -> tuple[str, str | None]:
        """Нормализовать результат в ``(raw DSL-строка, уточняющий вопрос | None)``.

        Вызов инструмента сворачивается в ``{"intent": ..., "params": ...}`` —
        ровно ту форму, которую валидирует :func:`parse_plan_json`.

        Свободный текст без JSON трактуется как уточняющий вопрос **только** при
        ``allow_text_clarification`` (tool calling: модель осознанно не вызывает
        функцию, а задаёт вопрос). Для OpenAI-совместимых провайдеров не-JSON —
        это ошибка, которую ловит :func:`parse_plan_json` и чинит планировщик.
        """
        if result.tool_calls:
            call = result.tool_calls[0]
            raw = json.dumps(
                {"intent": call.name, "params": call.arguments}, ensure_ascii=False
            )
            return raw, None
        content = (result.content or "").strip()
        if not content:
            return "", None
        try:
            _extract_json_object(content)
        except ValueError:
            if allow_text_clarification:
                return content, content
            return content, None
        return content, None

    def plan(
        self,
        question: str,
        history: Sequence[dict[str, str]] | None = None,
        data_profile: dict[str, Any] | None = None,
    ) -> AgentDecision:
        """Получить решение по вопросу с одной попыткой «починки» JSON."""
        question = question.strip()
        if not question:
            return AgentDecision(ok=False, error="Пустой вопрос", source="llm")
        messages = build_messages(question, history, data_profile)
        tools = build_tools() if self.supports_tools else None
        try:
            result = self._complete(messages, tools)
        except LLMError as exc:
            return AgentDecision(
                ok=False,
                error=f"LLM недоступна: {exc}",
                source="llm",
            )
        raw, clarification = self._to_dsl(
            result, allow_text_clarification=self.supports_tools
        )
        if clarification is not None:
            plan = ClarificationPlan(question=clarification, missing=[], suggestions=[])
            return _decision_from_plan(plan, raw=clarification, source="llm")
        try:
            plan = parse_plan_json(raw)
        except (ValueError, ValidationError) as exc:
            repair = (
                f"Предыдущий ответ не прошёл валидацию: {_short_error(exc)}. "
                "Верни ТОЛЬКО корректный JSON-объект по схеме, без пояснений."
            )
            try:
                fixed = self._complete(
                    [
                        *messages,
                        {"role": "assistant", "content": raw},
                        {"role": "user", "content": repair},
                    ],
                    tools,
                )
                raw_fixed, clarification_fixed = self._to_dsl(
                    fixed, allow_text_clarification=self.supports_tools
                )
                if clarification_fixed is not None:
                    raise ValueError("Модель вернула уточнение вместо исправленного JSON")
                plan = parse_plan_json(raw_fixed)
                return _decision_from_plan(plan, raw=raw_fixed, source="llm")
            except (LLMError, ValueError, ValidationError) as second_exc:
                return AgentDecision(
                    ok=False,
                    error=f"Модель вернула невалидный JSON: {_short_error(second_exc)}",
                    raw_response=raw,
                    source="llm",
                )
        return _decision_from_plan(plan, raw=raw, source="llm")


def _short_error(error: BaseException) -> str:
    """Короткое человекочитаемое описание ошибки валидации/JSON."""
    if isinstance(error, ValidationError):
        parts = []
        for item in error.errors()[:3]:
            location = ".".join(str(part) for part in item.get("loc", ()))
            parts.append(f"{location}: {item.get('msg', '')}")
        return "; ".join(parts) or str(error)
    return str(error)[:300]


_LIMIT_DEFAULT: Final[int] = 5
_LIMIT_MAX: Final[int] = 1000

_LIMIT_EXPLICIT_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:топ|top|первые|first)\s*[-\s]?\s*(?<![\d.])((?!20\d{2}\b)[1-9]\d{0,3})(?![\d.])"
)
"""Явное указание количества: «топ 5», «топ-10», «первые 3».

Год после слова «топ» (например, «топ 2024») количеством не считается:
защита от регрессии «год попал в limit» (``limit=202`` из «2024»).
"""

_LIMIT_BEFORE_NOUN_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![\d.])((?!20\d{2}\b)[1-9]\d{0,3})(?![\d.])\s+"
    r"(?:лучш|худш|товар|позици|строк|контрагент|групп|клиент)"
)
"""Число перед существительным: «5 худших товаров», «20 позиций»."""

_LIMIT_STANDALONE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![\d.])((?!20\d{2}\b)[1-9]\d{0,2})(?![\d.])"
    r"(?!\s*(?:год|г\.|года|году|лет|период|"
    r"январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр|"
    r"квартал|месяц|полугод|недел|дн))"
)
"""Одиночное число 1–999, не входящее в дату/год/единицу периода."""


def _clamp_limit(value: int) -> int:
    """Привести количество строк к допустимому диапазону (1…1000)."""
    return value if 1 <= value <= _LIMIT_MAX else _LIMIT_DEFAULT


def _extract_limit(text: str) -> int:
    """Извлечь количество строк для топ/антитопа из текста вопроса.

    Числа внутри дат (``21.09.2023``) и годов (``2024``) игнорируются: без
    границ слова регулярка ``\\d{1,3}`` выхватывала ``202`` из ``2024`` и
    возвращала «топ-202». Явные указания («топ 5», «5 товаров») имеют
    приоритет; если количество не указано — 5.

    Examples
    --------
    >>> _extract_limit("какие 5 товаров продаются хуже за 2024 год")
    5
    >>> _extract_limit("какие товары продаются хуже за 2024 год, топ 5?")
    5
    >>> _extract_limit("какие товары продаются хуже за 2024 год?")
    5
    """
    for pattern in (_LIMIT_EXPLICIT_RE, _LIMIT_BEFORE_NOUN_RE, _LIMIT_STANDALONE_RE):
        match = pattern.search(text)
        if match:
            return _clamp_limit(int(match.group(1)))
    return _LIMIT_DEFAULT


_MONTHS: Final[dict[str, int]] = {
    "январ": 1,
    "феврал": 2,
    "март": 3,
    "апрел": 4,
    "ма": 5,
    "июн": 6,
    "июл": 7,
    "август": 8,
    "сентябр": 9,
    "октябр": 10,
    "ноябр": 11,
    "декабр": 12,
}


def _month_number(token: str) -> int | None:
    """Номер месяца по русскому корню («январ», «май»/«мая», «июн»)."""
    for key, number in _MONTHS.items():
        if token.startswith(key):
            return number
    return None


_MONTH_RANGE_RE: Final = re.compile(
    r"(январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)\w*"
    r"\s*(?:[-–—]|по)\s*"
    r"(январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)\w*"
    r"\s+(20\d{2})"
)


def _month_range_period(lowered: str) -> tuple[date, date] | None:
    """Период «январь–июнь 2024» или «с января по июнь 2024»."""
    match = _MONTH_RANGE_RE.search(lowered)
    if not match:
        return None
    start_month = _month_number(match.group(1))
    end_month = _month_number(match.group(2))
    year = int(match.group(3))
    if start_month is None or end_month is None or start_month > end_month:
        return None
    return (date(year, start_month, 1), _last_day_of_month(year, end_month))


_QUOTED_RE: Final = re.compile(r'["«]([^"»]+)["»]')
_GROUP_QUOTED_RE: Final = re.compile(
    r'(?:групп[аы]|категори[яи])\s+["«]([^"»]+)["»]', re.IGNORECASE
)
DEFAULT_START_DATE: Final = date(2023, 1, 1)
DEFAULT_END_DATE: Final = date(2024, 12, 31)
_AVERAGE_CHECK_RE: Final = re.compile(
    r"средн\w*\s+(?:чек|выручк\w*|оборот\w*)", re.IGNORECASE
)
_BASE_MORE_RE: Final = re.compile(
    r"(?:больше|выше|свыше)\s+(\d[\d\s]*(?:[.,]\d+)?)\s*(тыс\w*|к|млн|млрд)?",
    re.IGNORECASE,
)


def _extract_quoted(text: str) -> str | None:
    """Текст в кавычках («товар», "группа") — для product_filter/group_filter."""
    match = _QUOTED_RE.search(text or "")
    if not match:
        return None
    value = match.group(1).strip()
    return value or None


def _year_aware_suggestions(text: str) -> list[str]:
    """Варианты ответа под годы из вопроса — вместо статичных «2023 и 2024».

    Детерминированные правила не разобрали интент, но годы в тексте уже есть
    (например, «2022» или JSON-DSL с ``2022-01-01``). Подсказки должны
    продолжать вопрос, а не предлагать чужие периоды: берём годы из текста и
    подставляем их в шаблоны. Если годов нет — возвращаем пустой список, и
    вызывающий код использует стандартные примеры.
    """
    years = sorted({int(value) for value in re.findall(r"\b(20\d{2})\b", text or "")})
    if not years:
        return []
    primary = years[-1]
    base = years[0] if len(years) > 1 else primary - 1
    suggestions: list[str] = []
    if len(years) > 1 and years[0] != years[-1]:
        suggestions.append(f"Сравни топ-5 товаров за {years[0]} и {years[-1]} годы")
    suggestions.extend(
        [
            f"Топ-5 товаров по выручке за {primary} год",
            f"Сколько мы продали за {primary} год?",
            f"Покажи выручку по месяцам за {primary} год",
            f"У каких контрагентов упали продажи в {primary} по сравнению с {base}?",
            f"Какие товарные группы просели в {primary} по сравнению с {base}?",
            f"Покажи товары с отрицательной выручкой за {primary} год",
        ]
    )
    return suggestions


def _extract_min_base_revenue(text: str) -> float | None:
    """Число из условия «выручка/база больше N» (100, 100к, 1 млн)."""
    if not re.search(r"баз|выручк", text, re.IGNORECASE):
        return None
    match = _BASE_MORE_RE.search(text or "")
    if not match:
        return None
    number = match.group(1).replace(" ", "").replace(",", ".")
    try:
        value = float(number)
    except ValueError:
        return None
    unit = (match.group(2) or "").casefold()
    multiplier = 1.0
    if unit.startswith(("тыс", "к")):
        multiplier = 1_000.0
    elif unit.startswith("млн"):
        multiplier = 1_000_000.0
    elif unit.startswith("млрд"):
        multiplier = 1_000_000_000.0
    return value * multiplier


def _relative_years(lowered: str, today: date) -> list[int]:
    """Годы из относительных формулировок в порядке упоминания (без повторов).

    Нужна для сравнения двух периодов: «прошлый год по сравнению с
    позапрошлым» → ``[today.year - 1, today.year - 2]``. Для формулировок без
    второго периода возвращается список из одного года.
    """
    pattern = re.compile(r"позапрошл\w*|(?<!за)прошл\w*|(?:в\s+)?этом\s+год\w*")
    years: list[int] = []
    for match in pattern.finditer(lowered):
        token = match.group(0)
        if token.startswith("позапрошл"):
            year = today.year - 2
        elif "этом" in token:
            year = today.year
        else:
            year = today.year - 1
        if year not in years:
            years.append(year)
    return years


def _relative_year(lowered: str, today: date) -> int | None:
    """Год из относительной формулировки: «позапрошлый», «прошлый», «этот».

    Возвращает ``None``, если в тексте нет указания на относительный год.
    «Позапрошлый» проверяется раньше «прошлого», иначе подстрока «прошл»
    внутри «позапрошлого» дала бы неверный сдвиг.
    """
    years = _relative_years(lowered, today)
    return years[0] if years else None


def _relative_month_period(lowered: str, today: date) -> tuple[date, date] | None:
    """Период «в этом/прошлом месяце» относительно ``today`` (или ``None``)."""
    if not re.search(r"\b(?:этом|прошл\w*)\s+месяц\w*", lowered):
        return None
    if re.search(r"\bэтом\s+месяц\w*", lowered):
        start = today.replace(day=1)
    else:
        start = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    return start, _last_day_of_month(start.year, start.month)


class RuleBasedPlanner:
    """Детерминированный fallback без LLM: разбор типовых формулировок.

    Используется, когда ``LLM_PROVIDER=none``, и в тестах. Не заменяет LLM:
    покрывает типовые вопросы и честно возвращает ``clarification`` там, где
    не хватает данных.
    """

    def plan(
        self,
        question: str,
        history: Sequence[dict[str, str]] | None = None,
        data_profile: dict[str, Any] | None = None,
        *,
        today: date | None = None,
    ) -> AgentDecision:
        """Разобрать вопрос по правилам и вернуть :class:`AgentDecision`.

        Правила намеренно лояльные: приложение анализирует только продажи,
        поэтому слова «продажи/выручка» не обязательны, если тип вопроса уже
        понятен из формулировки («динамика по месяцам за 2023», «итог за 2024»).

        ``today`` — дата, от которой считаются относительные периоды («прошлый
        год», «в этом месяце»). По умолчанию — текущая дата; параметр нужен для
        воспроизводимых тестов.
        """
        del history  # правилам история не нужна
        text = (question or "").strip()
        if not text:
            return AgentDecision(ok=False, error="Пустой вопрос", source="rules")
        lowered = text.lower()
        if _AVERAGE_CHECK_RE.search(text):
            # Средний чек не поддерживается: честный отказ вместо «похожего» интента.
            message = (
                "Расчёт среднего чека не поддерживается. "
                "Доступны только суммарная выручка и количество."
            )
            return AgentDecision(
                ok=False,
                status="refuse",
                refusal_reason=message,
                source="rules",
            )
        metric: MetricName = "quantity" if re.search(r"штук|количеств|единиц", lowered) else "revenue"
        periods = self._extract_periods(lowered, data_profile, today=today)
        if len(periods) >= 2 and re.search(r"по сравнению с", lowered):
            # «в 2024 по сравнению с 2023» → база = 2023, оцениваемый = 2024
            periods = [periods[1], periods[0]]

        group_match = _GROUP_QUOTED_RE.search(text)
        quoted = (
            group_match.group(1).strip()
            if group_match
            else _extract_quoted(text)
        )
        group_context = bool(group_match or re.search(r"групп|категор", lowered))
        min_base_revenue = _extract_min_base_revenue(lowered)
        try:
            # 0. Отрицательная выручка (возвраты/сторно): проверяем ПЕРВОЙ, чтобы
            # «товары с отрицательной выручкой» не уходили в обычный антитоп.
            if re.search(r"отрицательн|возврат|сторно|убыт|корректировк", lowered):
                group_by = (
                    "counterparty"
                    if re.search(r"контрагент|клиент|партн[её]р", lowered)
                    else "product"
                )
                params: dict[str, Any] = {"group_by": group_by}
                params.update(_dates_to_bounds(periods))
                return self._make("negative_revenue", params, source="rules")
            # 1. Итог за период: «итог», «сколько», «общая сумма», «выручка за …».
            # Слова топ/динамики/сравнений исключаются — они уходят в свои интенты.
            # «Покажи продажи 2025» — период есть, разрез не указан: это безопасный
            # итог за период, а не повод переспрашивать (паритет с LLM-путём).
            generic_sales = bool(
                periods
                and re.search(r"\b20\d{2}\b", text)
                and re.search(r"продаж|выручк", lowered)
            )
            if (
                re.search(
                    r"сколько|общ(ая|ую) (сумм|выручк)|итог|выручк\w* за|сумм\w* за",
                    lowered,
                )
                or generic_sales
            ) and not re.search(
                r"топ|top|первые|лучш|худш|лидер|аутсайд|наименьш|наибольш|"
                r"контрагент|клиент|групп|категор|динамик|просел|выросл|упал|"
                r"по месяц|по квартал|по год|временн|тенденц|тренд|ряд",
                lowered,
            ):
                total_params: dict[str, Any] = _dates_to_bounds(periods) or self._default_bounds(
                    data_profile
                )
                if quoted and not group_context:
                    total_params["product_filter"] = quoted
                return self._make("total", total_params, source="rules")
            # 2. Динамика контрагентов. Само слово «контрагенты» уже задаёт
            # интент, но для сравнения нужны два периода: без них вернём
            # уточнение (missing period1/period2), а не ошибку.
            if re.search(r"контрагент|клиент|партн[её]р", lowered) and re.search(
                r"выросл|упал|просел|динамик|сравн|измен", lowered
            ):
                return self._dynamics(
                    "counterparty_dynamics",
                    metric,
                    periods,
                    only_drops=bool(re.search(r"упал|просел|снизил|снижен|уменьш|падени", lowered)),
                    min_base_revenue=min_base_revenue,
                    question=text,
                )
            # 3. Динамика товарных групп/категорий.
            if re.search(r"групп|категор", lowered) and re.search(
                r"просел|упал|выросл|упали|выросли|сравн|динамик|измен", lowered
            ):
                return self._dynamics(
                    "group_dynamics",
                    metric,
                    periods,
                    only_drops=bool(re.search(r"просел|упал", lowered)),
                    group_filter=quoted,
                    question=text,
                )
            # 4. Временной ряд. Второе условие («продажи/выручка») больше не
            # требуется: любое «по месяцам/кварталам/годам», «динамика»,
            # «тренд», «тенденция» в этом приложении и так про продажи.
            if re.search(r"по месяц|месяц|квартал|по год|годов|динамик|временн|тенденц|тренд|ряд", lowered):
                freq = "Q" if "квартал" in lowered else "Y" if re.search(r"по год|годов", lowered) else "M"
                series_params: dict[str, Any] = {"freq": freq, "metric": metric}
                series_params.update(_dates_to_bounds(periods))
                if quoted and not group_context:
                    series_params["product_filter"] = quoted
                return self._make("timeseries", series_params, source="rules")
            # 5. Сравнение топов двух периодов (L4L): «сравни топ-5 за 2023 и 2024».
            # Запросы про топ всегда сначала проверяем на два периода: если их два,
            # пользователь почти наверняка просит сравнить рейтинги, а не один топ
            # за суммарный диапазон.
            if len(periods) >= 2 and re.search(
                r"топ|top|лучш|худш|лидер|аутсайд|продают|рейтинг|первые|наибольш|наименьш",
                lowered,
            ):
                return self._make(
                    "compare_top_products",
                    {
                        "period1": list(periods[0]),
                        "period2": list(periods[1]),
                        "top_n": _extract_limit(lowered),
                        "metric": metric,
                    },
                    source="rules",
                )
            # 6. Топ/антитоп: «топ», «первые», «лучшие/худшие», «лидеры/
            # аутсайдеры», «продаются хуже/лучше».
            if re.search(
                r"топ|top|первые|лучш\w*|худш\w*|лидер\w*|аутсайд\w*|"
                r"наименьш\w*|наибольш\w*|плохо|худо|"
                r"продают|продаёт|продаются|прода[её]тся",
                lowered,
            ):
                limit = _extract_limit(lowered)
                ascending = bool(re.search(r"хуже|худш|аутсайд|наименьш|плохо|меньш", lowered))
                params = {"metric": metric, "limit": limit, "ascending": ascending}
                params.update(_dates_to_bounds(periods) or self._default_bounds(data_profile))
                if quoted:
                    if group_context:
                        params["group_filter"] = quoted
                    else:
                        params["product_filter"] = quoted
                return self._make("top_bottom_products", params, source="rules")
        except ValueError as exc:
            return AgentDecision(ok=False, error=str(exc), source="rules")
        return AgentDecision(
            ok=True,
            intent="clarification",
            clarification=ClarificationPlan(
                question="Уточните, что показать: топ товаров, динамику по месяцам, "
                "сравнение контрагентов, срез по товарным группам или общий итог?",
                missing=["intent"],
                suggestions=_year_aware_suggestions(text)
                or [INTENT_EXAMPLES[name] for name in INTENT_NAMES],
            ),
            dsl={
                "intent": "clarification",
                "question": "Уточните, что показать?",
                "suggestions": _year_aware_suggestions(text)
                or [INTENT_EXAMPLES[name] for name in INTENT_NAMES],
            },
            source="rules",
            status="clarify",
        )

    def _make(self, intent: str, params: dict[str, Any], *, source: str) -> AgentDecision:
        """Собрать решение из имени интента и параметров (с валидацией моделью)."""
        model_name: dict[str, type[BaseModel]] = {
            "top_bottom_products": TopBottomPlan,
            "compare_top_products": CompareTopsPlan,
            "negative_revenue": NegativeRevenuePlan,
            "timeseries": TimeseriesPlan,
            "counterparty_dynamics": CounterpartyDynamicsPlan,
            "group_dynamics": GroupDynamicsPlan,
            "total": TotalPlan,
        }
        plan = model_name[intent].model_validate({"intent": intent, "params": params})
        return _decision_from_plan(plan, raw="", source=source)

    def _dynamics(
        self,
        intent: str,
        metric: MetricName,
        periods: list[tuple[date, date]],
        *,
        only_drops: bool = False,
        min_base_revenue: float | None = None,
        group_filter: str | None = None,
        question: str = "",
    ) -> AgentDecision:
        """Построить динамику по двум периодам или попросить уточнение."""
        if len(periods) < 2:
            years_suggestions = _year_aware_suggestions(question)
            return AgentDecision(
                ok=True,
                intent="clarification",
                clarification=ClarificationPlan(
                    question="За какие два периода сравнить? Например, 2023 и 2024.",
                    missing=["period1", "period2"],
                    suggestions=years_suggestions
                    or [
                        "2023 и 2024",
                        "первое полугодие 2024 и второе полугодие 2024",
                    ],
                ),
                dsl={
                    "intent": "clarification",
                    "question": "За какие два периода сравнить?",
                    "suggestions": years_suggestions,
                },
                source="rules",
                status="clarify",
            )
        params: dict[str, Any] = {
            "period1": periods[0],
            "period2": periods[1],
            "metric": metric,
        }
        if intent in ("group_dynamics", "counterparty_dynamics"):
            params["only_drops"] = only_drops
        if min_base_revenue is not None and intent == "counterparty_dynamics":
            params["min_base_revenue"] = min_base_revenue
        if group_filter is not None and intent == "group_dynamics":
            params["group_filter"] = group_filter
        return self._make(intent, params, source="rules")

    @staticmethod
    def _default_bounds(data_profile: dict[str, Any] | None) -> dict[str, date]:
        """Даты по умолчанию, если пользователь их не назвал.

        Берём фактический диапазон из ``data_profile`` (``date_min``/``date_max``),
        иначе — фиксированный демо-период 2023-01-01…2024-12-31. Это делает
        офлайн-ответы предсказуемыми: без периода не считаем «всё, включая
        строки без даты».
        """
        if data_profile:
            try:
                start = _parse_date_value(data_profile["date_min"])
                end = _parse_date_value(data_profile["date_max"])
            except (KeyError, TypeError, ValueError):
                pass
            else:
                return {"start_date": start, "end_date": end}
        return {"start_date": DEFAULT_START_DATE, "end_date": DEFAULT_END_DATE}

    def _extract_periods(
        self,
        lowered: str,
        data_profile: dict[str, Any] | None = None,
        *,
        today: date | None = None,
    ) -> list[tuple[date, date]]:
        """Извлечь до двух периодов: даты → годы → относительные → «последний месяц».

        Относительные периоды («в этом месяце», «прошлый год», «позапрошлый
        год», «март позапрошлого года») считаются от ``today`` (по умолчанию —
        текущая дата), а не от дат витрины. Если в вопросе упомянуты два
        относительных года («прошлый год по сравнению с позапрошлым»),
        возвращаются оба периода — для интентов сравнения.
        """
        current_date = today or date.today()
        explicit = re.findall(r"\b(\d{2}\.\d{2}\.\d{4}|\d{4}-\d{2}-\d{2})\b", lowered)
        parsed = [_parse_date_value(value) for value in explicit]
        if len(parsed) >= 4:
            return [tuple(sorted(parsed[0:2])), tuple(sorted(parsed[2:4]))]  # type: ignore[list-item]
        if len(parsed) == 2:
            return [tuple(sorted(parsed))]  # type: ignore[list-item]

        month_range = _month_range_period(lowered)
        if month_range is not None:
            return [month_range]

        years = [int(year) for year in re.findall(r"\b(20\d{2})\b", lowered)]
        unique_years: list[int] = []
        for year in years:
            if year not in unique_years:
                unique_years.append(year)
        if len(unique_years) >= 2:
            return [
                (date(unique_years[0], 1, 1), date(unique_years[0], 12, 31)),
                (date(unique_years[1], 1, 1), date(unique_years[1], 12, 31)),
            ]
        if len(unique_years) == 1:
            return [(date(unique_years[0], 1, 1), date(unique_years[0], 12, 31))]

        month_match = re.search(
            r"\b(январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)\w*\s+(20\d{2})",
            lowered,
        )
        if month_match:
            month = _MONTHS[month_match.group(1)]
            year = int(month_match.group(2))
            last_day = _last_day_of_month(year, month)
            return [(date(year, month, 1), last_day)]

        relative_month = _relative_month_period(lowered, current_date)
        if relative_month is not None:
            return [relative_month]

        # Два относительных года — для сравнения периодов, например
        # «в прошлом году по сравнению с позапрошлым».
        relative_years = _relative_years(lowered, current_date)
        if len(relative_years) >= 2:
            return [
                (date(year, 1, 1), date(year, 12, 31))
                for year in relative_years[:2]
            ]

        relative_year = _relative_year(lowered, current_date)
        month_name = re.search(
            r"\b(январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)\w*",
            lowered,
        )
        if month_name is not None and relative_year is not None:
            month = _MONTHS[month_name.group(1)]
            return [(date(relative_year, month, 1), _last_day_of_month(relative_year, month))]
        if relative_year is not None:
            return [(date(relative_year, 1, 1), date(relative_year, 12, 31))]

        if re.search(r"последн\w+ (месяц|30 дней)", lowered) and data_profile:
            date_max = data_profile.get("date_max")
            if date_max:
                end = _parse_date_value(date_max)
                return [(end - timedelta(days=29), end)]

        return []


_NEGATIVE_REVENUE_RE: Final = re.compile(
    r"отрицательн|возврат|сторно|убыт|корректировк", re.IGNORECASE
)


def is_negative_revenue_question(question: str) -> bool:
    """Похож ли вопрос на запрос про отрицательную выручку/возвраты/сторно."""
    return bool(_NEGATIVE_REVENUE_RE.search(question or ""))


def guard_decision(
    decision: AgentDecision,
    question: str,
    *,
    today: date | None = None,
    data_profile: dict[str, Any] | None = None,
) -> AgentDecision:
    """Не дать планировщику подменить запрос про отрицательную выручку антитопом.

    Если вопрос явно про отрицательную выручку/возвраты/сторно, а LLM выбрала
    другой интент (например, ``top_bottom_products`` — «худшие товары»), решение
    пересобирается детерминированными правилами. Если правила не дают
    ``negative_revenue``, исходное решение сохраняется.
    """
    if decision.intent == "negative_revenue" or not is_negative_revenue_question(question):
        return decision
    fallback = RuleBasedPlanner().plan(
        question, data_profile=data_profile, today=today
    )
    if fallback.ok and fallback.intent == "negative_revenue":
        return fallback
    return decision


#: Явная просьба считать в штуках, а не в деньгах. Без этих слов метрика —
#: выручка: «самых продаваемых», «лидеры», «топ-10» говорят о выручке, а число
#: в «топ-10» — это ``limit``, а не количество штук (ДЕФЕКТ: GigaChat ставил
#: ``metric="quantity"`` на вопрос «Топ-10 самых продаваемых товаров за прошлый
#: год» и ответ строился по количеству).
_QUANTITY_METRIC_RE: Final = re.compile(
    r"штук|\bшт\b|шт\.|количеств|единиц|экземпляр", re.IGNORECASE
)


def is_quantity_question(question: str) -> bool:
    """Просит ли пользователь считать в штуках (а не в рублях)."""
    return bool(_QUANTITY_METRIC_RE.search(question or ""))


def guard_metric(decision: AgentDecision, question: str) -> AgentDecision:
    """Не дать планировщику считать по количеству, когда просили деньги.

    Правило одно для обоих путей (LLM-граф и офлайн-правила): ``metric`` — это
    измерение результата, и подменять его моделью нельзя. Если в вопросе нет
    явной просьбы о штуках, а решение пришло с ``metric="quantity"``, метрика
    **только** меняется на ``"revenue"``; интент, период, ``limit``, ``ascending``
    и фильтры остаются как есть. ``dsl`` правится вместе с ``params``, чтобы
    «Технические детали» не расходились с расчётом.

    Отказы, уточнения и решения без ``metric`` не трогаются.
    """
    if not decision.ok:
        return decision
    if not decision.intent or decision.intent == "clarification":
        return decision
    if decision.status in ("clarify", "refuse", "error"):
        return decision
    params = decision.params or {}
    if params.get("metric") != "quantity" or is_quantity_question(question):
        return decision
    corrected = {**params, "metric": "revenue"}
    dsl = dict(decision.dsl or {})
    dsl_params = dsl.get("params")
    if isinstance(dsl_params, dict):
        dsl["params"] = {**dsl_params, "metric": "revenue"}
    return replace(decision, params=corrected, dsl=dsl)


def _dates_to_bounds(periods: list[tuple[date, date]]) -> dict[str, Any]:
    """Взять самый широкий период из найденных и превратить в start/end."""
    if not periods:
        return {}
    starts = [pair[0] for pair in periods]
    ends = [pair[1] for pair in periods]
    return {"start_date": min(starts), "end_date": max(ends)}


def _last_day_of_month(year: int, month: int) -> date:
    """Последний день месяца (без сторонних библиотек)."""
    if month == 12:
        return date(year, 12, 31)

    return date(year, month + 1, 1) - timedelta(days=1)


def make_planner(settings: LLMSettings | None = None) -> LLMPlanner | RuleBasedPlanner:
    """Создать планировщик: LLM, если провайдер настроен, иначе правила."""
    settings = settings or settings_from_env()
    if settings.is_configured:
        return LLMPlanner(settings)
    return RuleBasedPlanner()


# --------------------------------------------------------------------------- #
# Проверка связки «ключ + сертификат + модель» без генерации токенов
# --------------------------------------------------------------------------- #
#: Слово названия модели, по которому ищется «та же линейка» (GigaChat).
_MODEL_FAMILY: Final = "gigachat"
#: Уровни линейки GigaChat: по ним модели сопоставляются между версиями.
_MODEL_LEVELS: Final[frozenset[str]] = frozenset({"lite", "pro", "max", "ultra"})


@dataclass(frozen=True)
class ProviderCheck:
    """Результат :func:`check_provider` для одного провайдера.

    Показывает, что именно проверено на живом API и что с этим не так —
    чтобы не угадывать по сообщению об ошибке из UI.
    """

    provider: str
    model: str
    ok: bool
    #: Модели, которые вернул провайдер; ``[]`` — провайдер их не сообщает.
    models: tuple[str, ...] = ()
    #: Модель из настроек найдена среди доступных (``None`` — проверить нечем).
    model_available: bool | None = None
    #: Ближайшая похожая доступная модель (для подсказки при опечатке/алиасе).
    suggestion: str = ""
    error: str = ""

    def describe(self) -> str:
        """Человекочитаемая строка результата (без секретов)."""
        head = f"{self.provider}: {self.model}"
        if not self.ok:
            return f"ОШИБКА  {head} — {self.error}"
        if self.model_available is False:
            hint = f"; возможно, имелась в виду «{self.suggestion}»" if self.suggestion else ""
            return f"ВНИМАНИЕ {head} — ключ рабочий, но этой модели в доступе нет{hint}"
        if self.suggestion:
            return f"OK      {head} — в списке провайдера это «{self.suggestion}»"
        available = ", ".join(self.models) if self.models else "провайдер не сообщает список"
        return f"OK      {head} — доступно: {available}"


def _model_tokens(model: str) -> set[str]:
    """Слова модели в нижнем регистре: ``GigaChat-2-Pro`` → ``{gigachat, 2, pro}``."""
    normalized = re.sub(r"[^0-9a-z]+", " ", model.lower())
    return {token for token in normalized.split() if token}


def _resolve_model(wanted: str, available: Sequence[str]) -> str:
    """Доступное название модели, равнозначное настроенному (``""`` — нет).

    Порядок: точное совпадение → один уровень при любом написании
    (``GigaChat-2-Pro`` ≡ ``GigaChat Pro`` ≡ ``GigaChat-Pro``). Номер версии не
    учитывается: кабинет показывает «GigaChat Pro», запрос принимает
    ``GigaChat-2-Pro``, и это одна и та же модель одного уровня.
    """
    lowered_wanted = wanted.strip().lower()
    for candidate in available:
        if candidate.strip().lower() == lowered_wanted:
            return candidate
    tokens = _model_tokens(wanted) - {_MODEL_FAMILY}
    wanted_levels = tokens & _MODEL_LEVELS
    if wanted_levels:
        for candidate in available:
            candidate_levels = (_model_tokens(candidate) - {_MODEL_FAMILY}) & _MODEL_LEVELS
            if candidate_levels == wanted_levels:
                return candidate
    return ""


def _closest_model(wanted: str, available: Sequence[str]) -> str:
    """Ближайшая доступная модель по общим словам (``""`` — похожих нет).

    Точного совпадения здесь ждать не приходится — функция вызывается, когда
    его нет. Уровень модели (``pro``/``max``/``ultra``/``lite``) важнее номера
    версии: ``GigaChat-Pro`` и ``GigaChat-2-Pro`` — один уровень, а
    ``GigaChat-2-Max`` от ``GigaChat-3-Ultra`` отличается и уровнем, и ценой.
    Поэтому общий уровень обязателен, иначе модели не сопоставляются.
    """
    wanted_tokens = _model_tokens(wanted) - {_MODEL_FAMILY}
    wanted_levels = wanted_tokens & _MODEL_LEVELS
    if not wanted_tokens:
        return ""
    best_model = ""
    best_score = 0.0
    for candidate in available:
        candidate_tokens = _model_tokens(candidate) - {_MODEL_FAMILY}
        if not candidate_tokens:
            continue
        if wanted_levels != (candidate_tokens & _MODEL_LEVELS):
            continue
        score = len(wanted_tokens & candidate_tokens) / len(wanted_tokens | candidate_tokens)
        if score > best_score:
            best_model, best_score = candidate, score
    return best_model


def _key_header_safe(api_key: str) -> bool:
    """Годится ли ключ в HTTP-заголовок ``Authorization`` (только ASCII, latin-1).

    Заголовки HTTP кодируются в latin-1, поэтому кириллица в ключе превращается
    в ``UnicodeEncodeError`` ещё до сети. Ловим это заранее: типичная причина —
    незаменённый текстовый плейсхолдер в ``.env``.
    """
    try:
        api_key.encode("latin-1")
    except UnicodeEncodeError:
        return False
    return True


def check_provider(settings: LLMSettings) -> ProviderCheck:
    """Проверить настройки провайдера живым запросом, не тратя токены.

    Для GigaChat: OAuth (проверяет ключ, ``GIGACHAT_SCOPE`` и сертификат НУЦ) и
    ``GET /v1/models`` — сверяет настроенную модель со списком доступных этой
    версии API. Для OpenAI-совместимых провайдеров (DeepSeek/OpenAI/ollama)
    запрос ``/models`` проверяет ключ и ``base_url``.

    Секреты в результат не попадают: только статус и текст ошибки провайдера.
    """
    if not settings.is_configured:
        return ProviderCheck(
            provider=settings.provider,
            model=settings.model,
            ok=False,
            error="провайдер не настроен: нет ключа (см. .env)",
        )
    if not _key_header_safe(settings.api_key):
        return ProviderCheck(
            provider=settings.provider,
            model=settings.model,
            ok=False,
            error=(
                "ключ содержит символы вне ASCII — вероятно, в .env остался "
                "текстовый плейсхолдер вместо authorization key"
            ),
        )
    try:
        client = make_client(settings)
        if isinstance(client, GigaChatClient):
            models = client.models()
            model_checked = True
        else:
            models = client.list_models()
            model_checked = False
    except LLMError as exc:
        return ProviderCheck(provider=settings.provider, model=settings.model, ok=False, error=str(exc))
    except (UnicodeEncodeError, ValueError) as exc:
        # Заголовки HTTP кодируются в latin-1: не-ASCII ключ падает здесь, а не
        # в сети. Показываем причину, а не трейсбек.
        return ProviderCheck(
            provider=settings.provider,
            model=settings.model,
            ok=False,
            error=f"ключ непригоден для HTTP-заголовка: {exc}",
        )
    available = tuple(models)
    model_available: bool | None = None
    suggestion = ""
    if model_checked and available:
        resolved = _resolve_model(settings.model, available)
        model_available = resolved != ""
        if not model_available:
            suggestion = _closest_model(settings.model, available)
        elif resolved != settings.model:
            suggestion = resolved
    return ProviderCheck(
        provider=settings.provider,
        model=settings.model,
        ok=True,
        models=available,
        model_available=model_available,
        suggestion=suggestion,
    )


def check_providers(env: dict[str, str] | None = None) -> list[ProviderCheck]:
    """Проверить всех настроенных провайдеров (ненастроенные не вызываются)."""
    results: list[ProviderCheck] = []
    for option in provider_catalog(env):
        if not option.configured:
            continue
        results.append(check_provider(option.settings))
    return results


# --------------------------------------------------------------------------- #
# Схема и синхронизация с analytics
# --------------------------------------------------------------------------- #
def export_schema(path: str) -> dict[str, Any]:
    """Сохранить JSON-схему интентов (для промпта, LLM-инструмента и тестов)."""
    schema: dict[str, Any] = {
        "intents": {
            name: {
                "description": INTENT_DESCRIPTIONS[name],
                "params": list(INTENT_PARAMETER_NAMES[name]),
                "example_question": INTENT_EXAMPLES[name],
            }
            for name in INTENT_NAMES
        },
        "clarification": "intent=clarification с вопросом и списком missing/suggestions",
        "format": '{"intent": "<name>", "params": {...}}',
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(schema, handle, ensure_ascii=False, indent=2)
    return schema


def check_schema_sync() -> None:
    """Проверить, что модели совпадают с ``analytics.INTENT_PARAMETERS``.

    Требует pandas-интерпретатор (импортирует :mod:`analytics`). Вызывается в
    приложении при старте и в тестах на полном окружении.

    Raises
    ------
    RuntimeError
        Если наборы интентов или параметров разошлись.
    """
    from onec_analyst.analytics import INTENT_PARAMETERS  # локальный импорт: нужен pandas

    if set(INTENT_PARAMETER_NAMES) != set(INTENT_PARAMETERS):
        raise RuntimeError(
            "llm_agent и analytics расходятся по интентам: "
            f"{sorted(INTENT_PARAMETER_NAMES)} vs {sorted(INTENT_PARAMETERS)}"
        )
    for intent, fields in INTENT_PARAMETER_NAMES.items():
        if set(fields) != set(INTENT_PARAMETERS[intent]):
            raise RuntimeError(
                f"Интент {intent}: параметры llm_agent {sorted(fields)} "
                f"не совпадают с analytics {sorted(INTENT_PARAMETERS[intent])}"
            )


# --------------------------------------------------------------------------- #
# CLI для самопроверки
# --------------------------------------------------------------------------- #
def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LLM-слой: промпт, схема, проверка провайдеров")
    parser.add_argument("--show-prompt", action="store_true", help="напечатать системный промпт")
    parser.add_argument("--export-schema", metavar="PATH", help="сохранить JSON-схему интентов")
    parser.add_argument(
        "--check",
        action="store_true",
        help="проверить настроенных провайдеров живыми запросами (OAuth + /models)",
    )
    args = parser.parse_args(argv)
    failed = False
    if args.show_prompt:
        print(build_system_prompt())
    if args.export_schema:
        schema = export_schema(args.export_schema)
        print(f"Схема сохранена: {args.export_schema} ({len(schema['intents'])} интентов)")
    if args.check:
        results = check_providers()
        if not results:
            print("Нет настроенных провайдеров: задайте ключ в .env")
            return 1
        for result in results:
            print(result.describe())
            if not result.ok or result.model_available is False:
                failed = True
    if not args.show_prompt and not args.export_schema and not args.check:
        parser.print_help()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
