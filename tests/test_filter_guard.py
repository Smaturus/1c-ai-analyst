"""Guard на галлюцинированный фильтр модели (DECISIONS #19).

Модель может выдумать ``product_filter``/``group_filter``, которого нет ни в
вопросе, ни в витрине (gemma3:4b при t=0 стабильно даёт ``"..."``). Guard не
выполняет запрос, а возвращает ``clarify`` с каноническим ``missing`` — вместо
молчаливого переписывания DSL (запрещено DECISIONS #11).
"""

from __future__ import annotations

from onec_analyst.agent.nodes import _apply_guards, find_hallucinated_filter
from onec_analyst.llm_agent import AgentDecision

CATALOG = {
    "product_filter": ["Ноутбук Beta", "Телевизор Zeta"],
    "group_filter": ["Бытовая техника"],
}


def test_filter_present_in_question_passes() -> None:
    """Значение фильтра — подстрока вопроса (ветка 1): не трогаем."""
    result = find_hallucinated_filter(
        "Покажи динамику продаж «Кофе» за 2024",
        {"product_filter": "Кофе"},
        CATALOG,
    )

    assert result is None


def test_filter_paraphrase_resolved_in_catalog_passes() -> None:
    """Фильтра нет в вопросе, но он резолвится в каталог (ветка 2): ок."""
    result = find_hallucinated_filter(
        "топ товаров 2023",
        {"product_filter": "Beta"},
        CATALOG,
    )

    assert result is None


def test_filter_neither_question_nor_catalog_is_hallucinated() -> None:
    """Фильтр-заглушка («...») — галлюцинация (ветка 3): вернуть ключ."""
    result = find_hallucinated_filter(
        "лучшие 2 товара 2023",
        {"product_filter": "..."},
        CATALOG,
    )

    assert result == "product_filter"


def test_group_filter_hallucinated() -> None:
    """То же для group_filter."""
    result = find_hallucinated_filter(
        "динамика продаж за год",
        {"group_filter": "..."},
        CATALOG,
    )

    assert result == "group_filter"


def test_guard_returns_clarify_not_silent_rewrite() -> None:
    """Галлюцинированный фильтр → clarify с каноническим missing."""
    decision = AgentDecision(
        ok=True,
        intent="top_bottom_products",
        params={"product_filter": "..."},
        dsl={"intent": "top_bottom_products", "params": {"product_filter": "..."}},
        source="llm",
        status="ok",
    )
    state = {
        "today": "2026-11-20",
        "result_entities": [],
        "filter_catalog": CATALOG,
    }

    resolved = _apply_guards("лучшие 2 товара 2023", decision, state)

    assert resolved.status == "clarify"
    assert resolved.intent == "clarification"
    assert resolved.clarification is not None
    assert resolved.clarification.missing == ["product_filter"]
