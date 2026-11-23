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

#: Расширенный каталог для правила границы (DECISIONS #30): в нём есть товар,
#: название которого начинается с категорийного слова («Кабель»), два
#: кондиционера (неоднозначность сохраняется) и однословный «Ноутбук Beta».
CATALOG_FULL = {
    "product_filter": [
        "Ноутбук Beta",
        "Телевизор Zeta",
        "Кабель-удлинитель «Iota»",
        "Кофеварка JACOBS (Австрия)",
        "Кондиционер ЕЛЕКТА",
        "Кондиционер ФИРМСТАР",
    ],
    "group_filter": ["Бытовая техника"],
}

QUESTION_WITHOUT_FILTER = "Покажи топ 5 товаров за 2023 год"


def test_filter_present_in_question_passes() -> None:
    """Значение фильтра — подстрока вопроса (ветка 1): не трогаем."""
    result = find_hallucinated_filter(
        "Покажи динамику продаж «Кабель» за 2024",
        {"product_filter": "Кабель"},
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


# --------------------------------------------------------------------------- #
# Правило границы: префикс названия — не парафраз (DECISIONS #30)
# --------------------------------------------------------------------------- #
def test_category_word_prefixing_product_name_is_hallucinated() -> None:
    r"""«Кабель» — префикс названия «Кабель-удлинитель «Iota»», а не сущность.

    Целевой кейс gemma3:4b: фильтра в вопросе нет, модель подставила
    категорийное слово. Токенизация ``\w+`` режет «Кабель-удлинитель» по дефису,
    поэтому «Кабель» — отдельный токен, и резолюция в каталог СРАБАТЫВАЕТ
    (ветка 3: резолв есть, но это строгий префикс).
    """
    result = find_hallucinated_filter(
        QUESTION_WITHOUT_FILTER,
        {"product_filter": "Кабель"},
        CATALOG_FULL,
    )

    assert result == "product_filter"


def test_bare_first_token_prefix_is_hallucinated() -> None:
    """Голый первый токен названия («Ноутбук») — тоже префикс, не парафраз."""
    result = find_hallucinated_filter(
        QUESTION_WITHOUT_FILTER,
        {"product_filter": "Ноутбук"},
        CATALOG_FULL,
    )

    assert result == "product_filter"


def test_suffix_token_paraphrase_passes() -> None:
    """Frozen №2 не ломается: «Beta» → «Ноутбук Beta» — часть названия, не префикс."""
    result = find_hallucinated_filter(
        "топ товаров 2023",
        {"product_filter": "Beta"},
        CATALOG_FULL,
    )

    assert result is None


def test_full_names_pass_in_both_forms() -> None:
    """Точное название и его нормализованная форма (регистр) — не префикс."""
    normalized = find_hallucinated_filter(
        QUESTION_WITHOUT_FILTER,
        {"product_filter": "кондиционер ЕЛЕКТА"},
        CATALOG_FULL,
    )
    exact = find_hallucinated_filter(
        QUESTION_WITHOUT_FILTER,
        {"product_filter": "Кабель-удлинитель «Iota»"},
        CATALOG_FULL,
    )

    assert normalized is None
    assert exact is None


def test_plural_paraphrase_stays_clarified() -> None:
    """«кондиционеры» по-прежнему clarify: резолюция неоднозначна (2 товара)."""
    result = find_hallucinated_filter(
        QUESTION_WITHOUT_FILTER,
        {"product_filter": "кондиционеры"},
        CATALOG_FULL,
    )

    assert result == "product_filter"
