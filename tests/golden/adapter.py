"""Адаптер «эталонный формат (ТЗ 6.3) → канон 6.4» (Приложение A).

Эталон `tests/golden/GoldenSet.py` хранит ожидания в формате ТЗ 6.3:
``direction``, ``granularity``, ``dimension``, ``metrics``, ``products``,
строки периодов ``"A_B"``. Канон (``analytics.py``/Pydantic) использует
``ascending``, ``freq``, ``group_by``, ``metric``, ``top_n`` и пары дат.

Адаптер — единственное место, где этот маппинг записан. Из него генерируется
``cases.json`` (см. ``tools/sync_golden_set.py``); CI сравнивает производную с
генерацией, а не с сырым файлом.

Параметры, которые в 6.4 не выражаются (``products`` у compare, ``revenue_vat``),
поднимают :class:`NonMappable`: такие кейсы помечаются ``xfail``/``refuse`` по
решению в ``docs/DECISIONS.md``.
"""

from __future__ import annotations

from typing import Any

#: ``direction`` эталона → ``ascending`` канона.
_DIRECTION = {"top": False, "bottom": True}

#: ``granularity`` эталона → ``freq`` канона.
_GRANULARITY = {"day": "D", "week": "W", "month": "M", "quarter": "Q", "year": "Y"}


class NonMappable(ValueError):
    """Параметр эталона не выражается в каноне 6.4 (кейс → xfail/refuse)."""


def _period_pair(value: Any) -> Any:
    """``"2023-01-01_2023-12-31"`` → ``["2023-01-01", "2023-12-31"]``."""
    if isinstance(value, str) and "_" in value:
        start, end = value.split("_", 1)
        return [start, end]
    return value


def _single_filter(value: Any, key: str) -> Any:
    """Список фильтра → одна строка; несколько значений — вне MVP."""
    if isinstance(value, list):
        if len(value) != 1:
            raise NonMappable(f"{key}: список > одного значения вне MVP")
        return value[0]
    return value


#: Семантические ключи ``missing`` → канонические ключи ``ClarificationPlan``.
_MISSING_REMAP = {"period": "start_date", "products": "product_filter"}


def adapt_facts(facts: Any) -> list[dict[str, Any]]:
    """Ремап ``missing`` у clarify-фактов к каноническим ключам ClarificationPlan."""
    out: list[dict[str, Any]] = []
    for fact in facts or []:
        fact = dict(fact)
        if fact.get("type") == "clarify" and fact.get("missing") in _MISSING_REMAP:
            fact["missing"] = _MISSING_REMAP[fact["missing"]]
        out.append(fact)
    return out


def adapt_expected_dsl(case: dict[str, Any]) -> dict[str, Any] | None:
    """Канонический ``expected_dsl`` из эталонного кейса (``None`` для UNKNOWN).

    Raises
    ------
    NonMappable
        Если ожидание использует не выражаемое в 6.4 (products/revenue_vat/…).
    """
    dsl = case.get("expected_dsl")
    if dsl is None:
        return None
    intent = dsl.get("intent")
    params = dict(dsl.get("params") or {})

    if "direction" in params:
        params["ascending"] = _DIRECTION[params.pop("direction")]

    if "granularity" in params:
        params["freq"] = _GRANULARITY[params.pop("granularity")]

    if "dimension" in params:
        params["group_by"] = params.pop("dimension")

    if "metrics" in params:
        metrics = params.pop("metrics")
        if "revenue_vat" in metrics:
            raise NonMappable("revenue_vat вне MVP")
        if intent != "total":
            params["metric"] = metrics[0]
        # у total метрики нет: факт по quantity проверяется отдельно

    if "products" in params:
        raise NonMappable("products у compare_top_products вне MVP")

    for key in ("period1", "period2"):
        if key in params:
            params[key] = _period_pair(params[key])

    if intent == "compare_top_products" and "limit" in params:
        params["top_n"] = params.pop("limit")

    for key in ("product_filter", "group_filter"):
        if key in params:
            params[key] = _single_filter(params[key], key)

    if intent == "counterparty_dynamics" and params.get("product_filter"):
        raise NonMappable("product_filter у counterparty_dynamics вне MVP")

    return {"intent": intent, "params": params}


__all__ = ["NonMappable", "adapt_expected_dsl"]
