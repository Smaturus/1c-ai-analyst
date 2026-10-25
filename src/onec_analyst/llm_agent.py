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
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Final, Literal, Sequence

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
)

__all__ = [
    "INTENT_NAMES",
    "INTENT_PARAMETER_NAMES",
    "INTENT_DESCRIPTIONS",
    "INTENT_EXAMPLES",
    "ClarificationPlan",
    "CompareTopsParams",
    "CompareTopsPlan",
    "NegativeRevenueParams",
    "NegativeRevenuePlan",
    "IntentDSL",
    "LLMSettings",
    "LLMClient",
    "LLMError",
    "LLMPlanner",
    "RuleBasedPlanner",
    "AgentDecision",
    "settings_from_env",
    "build_system_prompt",
    "build_messages",
    "parse_plan_json",
    "to_dispatch_params",
    "is_negative_revenue_question",
    "guard_decision",
    "make_planner",
    "check_schema_sync",
    "export_schema",
    "SYSTEM_PROMPT",
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
    limit: int = Field(default=10, ge=1, le=1000)
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
    dsl:
        JSON-безопасное представление DSL (для показа пользователю).
    source:
        ``"llm"`` или ``"rules"`` — чем получен DSL.
    """

    ok: bool
    intent: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    clarification: ClarificationPlan | None = None
    error: str | None = None
    raw_response: str = ""
    dsl: dict[str, Any] = field(default_factory=dict)
    source: str = "rules"


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
    intent="clarification" с вопросом «Расчёт среднего чека не поддерживается.
    Доступны только суммарная выручка и количество.» Не подменяй такой запрос
    топом, итогом или динамикой.

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
        Ключ API (для ollama не нужен).
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
    "ollama": "llama3.1",
}
_DEFAULT_BASE_URLS: Final[dict[str, str]] = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "ollama": "http://localhost:11434/v1",
}

_DOTENV_FILE: Final = Path(__file__).resolve().parent / ".env"


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


def settings_from_env(env: dict[str, str] | None = None) -> LLMSettings:
    """Прочитать настройки LLM из окружения (с разумными значениями по умолчанию)."""
    values = env if env is not None else _environment_with_dotenv()
    provider = (values.get("LLM_PROVIDER") or "none").strip().lower()
    if provider not in ("none", "openai", "deepseek", "ollama"):
        raise ValueError(
            f"Неизвестный LLM_PROVIDER={provider!r}; допустимо: none, openai, deepseek, ollama"
        )
    model = (values.get("LLM_MODEL") or _DEFAULT_MODELS.get(provider, "")).strip()
    base_url = (values.get("LLM_BASE_URL") or _DEFAULT_BASE_URLS.get(provider, "")).strip()
    json_mode = (values.get("LLM_JSON_MODE") or ("0" if provider == "ollama" else "1")) != "0"
    try:
        timeout = float(values.get("LLM_TIMEOUT", "60"))
        temperature = float(values.get("LLM_TEMPERATURE", "0"))
    except ValueError as exc:  # pragma: no cover - защита от кривого env
        raise ValueError(f"LLM_TIMEOUT/LLM_TEMPERATURE должны быть числами: {exc}") from exc
    return LLMSettings(
        provider=provider,
        model=model,
        api_key=(values.get("LLM_API_KEY") or "").strip(),
        base_url=base_url.rstrip("/"),
        timeout=timeout,
        temperature=temperature,
        json_mode=json_mode,
    )


class LLMError(RuntimeError):
    """Ошибка вызова LLM (сеть, HTTP-статус, пустой ответ)."""


class LLMClient:
    """Минимальный OpenAI-совместимый клиент на стандартной библиотеке.

    Работает с OpenAI, DeepSeek и Ollama (``/v1``) — без внешних зависимостей.
    """

    def __init__(self, settings: LLMSettings) -> None:
        self._settings = settings

    def chat(self, messages: list[dict[str, str]]) -> str:
        """Отправить сообщения и вернуть текст ответа модели.

        Raises
        ------
        LLMError
            Сеть недоступна, HTTP-ошибка, пустой ответ или провайдер не настроен.
        """
        settings = self._settings
        if not settings.is_configured or not settings.base_url:
            raise LLMError("LLM не настроена: задайте LLM_PROVIDER/LLM_API_KEY/LLM_BASE_URL")
        payload: dict[str, Any] = {
            "model": settings.model,
            "messages": messages,
            "temperature": settings.temperature,
        }
        if settings.json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json"}
        if settings.api_key:
            headers["Authorization"] = f"Bearer {settings.api_key}"
        url = f"{settings.base_url}/chat/completions"
        try:
            answer = self._post(url, payload, headers)
        except LLMError as exc:
            # Некоторые провайдеры не поддерживают response_format — повторяем без него.
            if settings.json_mode and "response_format" in str(exc):
                payload.pop("response_format", None)
                answer = self._post(url, payload, headers)
            else:
                raise
        return answer

    def _post(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> str:
        """POST-запрос к Chat Completions и извлечение текста ответа."""
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._settings.timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")[:500]
            raise LLMError(f"HTTP {exc.code}: {details}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LLMError(f"Сеть недоступна: {exc}") from exc
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise LLMError(f"Провайдер вернул не JSON: {body[:200]!r}") from exc
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"Неожиданный ответ провайдера: {body[:300]!r}") from exc
        if not isinstance(content, str) or not content.strip():
            raise LLMError("Провайдер вернул пустой ответ")
        return content


# --------------------------------------------------------------------------- #
# Планировщики: LLM и детерминированный fallback
# --------------------------------------------------------------------------- #
class LLMPlanner:
    """Планировщик на LLM: вопрос → сообщения → JSON → Pydantic → решение."""

    def __init__(self, settings: LLMSettings, client: LLMClient | None = None) -> None:
        self._settings = settings
        self._client = client or LLMClient(settings)

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
        try:
            raw = self._client.chat(messages)
        except LLMError as exc:
            return AgentDecision(
                ok=False,
                error=f"LLM недоступна: {exc}",
                source="llm",
            )
        try:
            plan = parse_plan_json(raw)
        except (ValueError, ValidationError) as exc:
            repair = (
                f"Предыдущий ответ не прошёл валидацию: {_short_error(exc)}. "
                "Верни ТОЛЬКО корректный JSON-объект по схеме, без пояснений."
            )
            try:
                raw_fixed = self._client.chat(
                    [*messages, {"role": "assistant", "content": raw}, {"role": "user", "content": repair}]
                )
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


_LIMIT_DEFAULT: Final[int] = 10
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
    приоритет; если количество не указано — 10.

    Examples
    --------
    >>> _extract_limit("какие 5 товаров продаются хуже за 2024 год")
    5
    >>> _extract_limit("какие товары продаются хуже за 2024 год, топ 5?")
    5
    >>> _extract_limit("какие товары продаются хуже за 2024 год?")
    10
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
                ok=True,
                intent="clarification",
                clarification=ClarificationPlan(
                    question=message,
                    missing=["intent"],
                    suggestions=[],
                ),
                dsl={"intent": "clarification", "question": message},
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
            if re.search(
                r"сколько|общ(ая|ую) (сумм|выручк)|итог|выручк\w* за|сумм\w* за",
                lowered,
            ) and not re.search(
                r"топ|top|первые|лучш|худш|лидер|аутсайд|наименьш|наибольш|"
                r"контрагент|клиент|групп|категор|динамик|просел|выросл|упал|"
                r"по месяц|по квартал|по год|временн|тенденц|тренд|ряд",
                lowered,
            ):
                total_params = _dates_to_bounds(periods) or self._default_bounds(
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
                )
            # 4. Временной ряд. Второе условие («продажи/выручка») больше не
            # требуется: любое «по месяцам/кварталам/годам», «динамика»,
            # «тренд», «тенденция» в этом приложении и так про продажи.
            if re.search(r"по месяц|месяц|квартал|по год|годов|динамик|временн|тенденц|тренд|ряд", lowered):
                freq = "Q" if "квартал" in lowered else "Y" if re.search(r"по год|годов", lowered) else "M"
                params: dict[str, Any] = {"freq": freq, "metric": metric}
                params.update(_dates_to_bounds(periods))
                if quoted and not group_context:
                    params["product_filter"] = quoted
                return self._make("timeseries", params, source="rules")
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
                suggestions=[INTENT_EXAMPLES[name] for name in INTENT_NAMES],
            ),
            dsl={
                "intent": "clarification",
                "question": "Уточните, что показать?",
                "suggestions": [INTENT_EXAMPLES[name] for name in INTENT_NAMES],
            },
            source="rules",
        )

    def _make(self, intent: str, params: dict[str, Any], *, source: str) -> AgentDecision:
        """Собрать решение из имени интента и параметров (с валидацией моделью)."""
        model_name = {
            "top_bottom_products": TopBottomPlan,
            "compare_top_products": CompareTopsPlan,
            "negative_revenue": NegativeRevenuePlan,
            "timeseries": TimeseriesPlan,
            "counterparty_dynamics": CounterpartyDynamicsPlan,
            "group_dynamics": GroupDynamicsPlan,
            "total": TotalPlan,
        }[intent]
        plan = model_name.model_validate({"intent": intent, "params": params})
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
    ) -> AgentDecision:
        """Построить динамику по двум периодам или попросить уточнение."""
        if len(periods) < 2:
            return AgentDecision(
                ok=True,
                intent="clarification",
                clarification=ClarificationPlan(
                    question="За какие два периода сравнить? Например, 2023 и 2024.",
                    missing=["period1", "period2"],
                    suggestions=["2023 и 2024", "первое полугодие 2024 и второе полугодие 2024"],
                ),
                dsl={"intent": "clarification", "question": "За какие два периода сравнить?"},
                source="rules",
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
) -> AgentDecision:
    """Не дать планировщику подменить запрос про отрицательную выручку антитопом.

    Если вопрос явно про отрицательную выручку/возвраты/сторно, а LLM выбрала
    другой интент (например, ``top_bottom_products`` — «худшие товары»), решение
    пересобирается детерминированными правилами. Если правила не дают
    ``negative_revenue``, исходное решение сохраняется.
    """
    if decision.intent == "negative_revenue" or not is_negative_revenue_question(question):
        return decision
    fallback = RuleBasedPlanner().plan(question, today=today)
    if fallback.ok and fallback.intent == "negative_revenue":
        return fallback
    return decision


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
    parser = argparse.ArgumentParser(description="LLM-слой: промпт, схема, самопроверка")
    parser.add_argument("--show-prompt", action="store_true", help="напечатать системный промпт")
    parser.add_argument("--export-schema", metavar="PATH", help="сохранить JSON-схему интентов")
    args = parser.parse_args(argv)
    if args.show_prompt:
        print(build_system_prompt())
    if args.export_schema:
        schema = export_schema(args.export_schema)
        print(f"Схема сохранена: {args.export_schema} ({len(schema['intents'])} интентов)")
    if not args.show_prompt and not args.export_schema:
        parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(_main())
