"""Схема и валидация нормализованного GoldSet (ТЗ 6.4, ADR 0013).

Набор хранится в ``tests/golden/cases.json`` в каноническом DSL: имена и
параметры совпадают с ``analytics.INTENT_PARAMETERS`` и Pydantic-моделями
``llm_agent``. Валидация ловит возврат устаревших обозначений (direction,
metrics, products и т.п.) и рассинхронизацию ``expected_status`` с фактами.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from pydantic import ValidationError

from onec_analyst.llm_agent import INTENT_NAMES, parse_plan_json

GOLDEN_DIR = Path(__file__).resolve().parent
CASES_PATH = GOLDEN_DIR / "cases.json"
FIXTURE_PATH = GOLDEN_DIR.parent / "fixtures" / "golden_sales.csv"

ALLOWED_CATEGORIES = frozenset({"simple", "context", "edge_case", "non_analytical"})
ALLOWED_STATUSES = frozenset({"ok", "clarify", "refuse", "error"})
ALLOWED_METRICS = frozenset({"revenue", "quantity"})

#: Заглушки в expected_facts: конкретную сущность не проверяем.
GENERIC_ITEMS = frozenset(
    {"товар", "товары", "контрагент", "контрагенты", "группа", "группы", "все"}
)

#: Ключи редакции 6.3, которых не должно быть в каноническом DSL.
#: result_entities проверяется отдельно: это допустимое поле кейса, но не DSL.
LEGACY_KEYS = frozenset(
    {"direction", "dimension", "granularity", "metrics", "products"}
)

#: Семантический ключ missing → допустимые канонические ключи.
MISSING_ALIASES: Mapping[str, frozenset[str]] = {
    "period": frozenset({"period", "period1", "period2", "start_date", "end_date"}),
    "products": frozenset({"products", "product_filter"}),
    "group": frozenset({"group", "group_filter"}),
    "counterparty": frozenset({"counterparty"}),
    "period1": frozenset({"period1"}),
    "period2": frozenset({"period2"}),
    "start_date": frozenset({"start_date"}),
    "end_date": frozenset({"end_date"}),
    "product_filter": frozenset({"product_filter", "products"}),
    "group_filter": frozenset({"group_filter", "group"}),
    "intent": frozenset({"intent"}),
}

_ID_RE = re.compile(r"^test_\d{3}_[a-z0-9_]+$")


def load_document(path: Path | None = None) -> dict[str, Any]:
    """Прочитать ``cases.json`` целиком (с версией набора)."""
    target = path or CASES_PATH
    return json.loads(target.read_text(encoding="utf-8"))


def load_cases(path: Path | None = None) -> list[dict[str, Any]]:
    """Список кейсов без служебной обёртки."""
    document = load_document(path)
    cases = document.get("cases")
    if not isinstance(cases, list):
        raise ValueError("cases.json: поле 'cases' должно быть списком")
    return cases


def walk_keys(value: Any) -> Iterator[str]:
    """Все ключи словарей рекурсивно (для поиска legacy-полей)."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from walk_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk_keys(item)


def canonical_params(dsl: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """Провалидировать DSL и вернуть (intent, params с дефолтами)."""
    plan = parse_plan_json(json.dumps(dsl, ensure_ascii=False))
    payload = plan.model_dump(mode="json", exclude_none=True)
    return str(payload["intent"]), dict(payload.get("params", {}))


def _validate_dsl(
    name: str,
    dsl: Any,
    errors: list[str],
    *,
    allow_none: bool,
) -> tuple[str, dict[str, Any]] | None:
    if dsl is None:
        if not allow_none:
            errors.append(f"{name}: DSL обязателен")
        return None
    if not isinstance(dsl, dict):
        errors.append(f"{name}: DSL должен быть объектом")
        return None
    legacy = sorted(set(walk_keys(dsl)) & LEGACY_KEYS)
    if legacy:
        errors.append(f"{name}: устаревшие ключи {legacy}")
    if "result_entities" in dsl:
        errors.append(f"{name}: result_entities должны быть вне DSL")
    try:
        intent, params = canonical_params(dsl)
    except (ValidationError, ValueError, TypeError) as exc:
        errors.append(f"{name}: не проходит Pydantic: {exc}")
        return None
    if intent not in INTENT_NAMES:
        errors.append(f"{name}: неизвестный интент {intent!r}")
        return None
    if "intent" in dsl and dsl["intent"] != intent:
        errors.append(f"{name}: intent не совпал после нормализации")
    return intent, params


def _validate_fact(fact: Any, index: int, errors: list[str]) -> str | None:
    if not isinstance(fact, dict):
        errors.append(f"expected_facts[{index}]: факт должен быть объектом")
        return None
    fact_type = fact.get("type")
    if fact_type == "value":
        for key in ("item", "metric", "period"):
            if not isinstance(fact.get(key), str) or not fact[key].strip():
                errors.append(f"expected_facts[{index}]: value без {key}")
        if fact.get("metric") not in ALLOWED_METRICS:
            errors.append(
                f"expected_facts[{index}]: metric {fact.get('metric')!r} "
                f"не поддерживается"
            )
        if "value" in fact and not isinstance(fact["value"], (int, float)):
            errors.append(f"expected_facts[{index}]: value должно быть числом")
        return "value"
    if fact_type == "refusal":
        markers = fact.get("reason_contains")
        if markers is not None and (
            not isinstance(markers, list)
            or not markers
            or any(not isinstance(marker, str) or not marker for marker in markers)
        ):
            errors.append(
                f"expected_facts[{index}]: reason_contains должен быть непустым списком строк"
            )
        return "refusal"
    if fact_type == "clarify":
        missing = fact.get("missing")
        if not isinstance(missing, str) or missing not in MISSING_ALIASES:
            errors.append(
                f"expected_facts[{index}]: неизвестный missing {missing!r}; "
                f"допустимо: {sorted(MISSING_ALIASES)}"
            )
        return "clarify"
    errors.append(f"expected_facts[{index}]: неизвестный тип {fact_type!r}")
    return None


def validate_case(case: Mapping[str, Any]) -> list[str]:
    """Вернуть список ошибок кейса; пустой список — кейс корректен."""
    errors: list[str] = []
    case_id = case.get("id")
    if not isinstance(case_id, str) or not _ID_RE.match(case_id):
        errors.append(f"id: неверный формат {case_id!r}")
    if case.get("category") not in ALLOWED_CATEGORIES:
        errors.append(f"category: недопустимая {case.get('category')!r}")
    question = case.get("question")
    if not isinstance(question, str) or not question.strip():
        errors.append("question: пустой вопрос")

    status = case.get("expected_status")
    if status not in ALLOWED_STATUSES:
        errors.append(f"expected_status: недопустимый {status!r}")

    mock_today = case.get("mock_today")
    if mock_today is not None:
        try:
            date.fromisoformat(str(mock_today))
        except ValueError:
            errors.append(f"mock_today: не дата ISO {mock_today!r}")

    entities = case.get("result_entities")
    if entities is not None and (
        not isinstance(entities, list)
        or any(not isinstance(item, str) or not item for item in entities)
    ):
        errors.append("result_entities: должен быть списком непустых строк")

    expected_dsl = case.get("expected_dsl")
    parsed = _validate_dsl("expected_dsl", expected_dsl, errors, allow_none=True)
    history = case.get("history_dsl")
    _validate_dsl("history_dsl", history, errors, allow_none=True)

    expected_intent = case.get("expected_intent")
    if status == "ok":
        if parsed is None:
            errors.append("expected_status=ok, но expected_dsl отсутствует")
        else:
            intent, _ = parsed
            if expected_intent != intent:
                errors.append(
                    f"expected_intent {expected_intent!r} не совпадает с DSL {intent!r}"
                )
    else:
        if expected_dsl is not None:
            errors.append(f"expected_status={status}, но expected_dsl не None")
        if expected_intent != "UNKNOWN":
            errors.append(f"expected_status={status}, expected_intent должен быть UNKNOWN")

    facts = case.get("expected_facts")
    fact_types: list[str] = []
    if not isinstance(facts, list) or not facts:
        errors.append("expected_facts: нужен непустой список")
    else:
        if len(facts) > 4:
            errors.append("expected_facts: больше 4 фактов")
        for index, fact in enumerate(facts):
            fact_type = _validate_fact(fact, index, errors)
            if fact_type:
                fact_types.append(fact_type)

    if status == "ok":
        if "value" not in fact_types:
            errors.append("expected_status=ok, но нет ни одного value-факта")
    elif status == "clarify":
        if "clarify" not in fact_types:
            errors.append("expected_status=clarify, но нет clarify-факта")
    elif status == "refuse":
        if "refusal" not in fact_types:
            errors.append("expected_status=refuse, но нет refusal-факта")

    if case.get("xfail"):
        if not isinstance(case.get("xfail_reason"), str) or not case["xfail_reason"].strip():
            errors.append("xfail без xfail_reason")

    if "notes" in case and not isinstance(case["notes"], str):
        errors.append("notes: должно быть строкой")

    return errors


def period_pairs(case: Mapping[str, Any]) -> Iterable[tuple[str, Any]]:
    """Пары (ключ, значение) period1/period2 из expected_dsl и history_dsl."""
    for source in ("expected_dsl", "history_dsl"):
        dsl = case.get(source)
        if not isinstance(dsl, dict):
            continue
        params = dsl.get("params") or {}
        for key in ("period1", "period2"):
            if key in params:
                yield f"{source}.{key}", params[key]
