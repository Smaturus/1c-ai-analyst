"""Тесты выбора текста запроса после уточнения.

Модуль `clarification` решает: вторая часть самодостаточна (полный вопрос) или это
фрагмент, который нужно склеить с исходным вопросом. Здесь проверяются правила,
склейка и LLM-классификатор (через fake-клиент, без сети).
"""

from __future__ import annotations

from datetime import date

import pytest

from clarification import (
    SOURCE_ANSWER,
    SOURCE_LLM,
    SOURCE_MERGED,
    answer_is_self_sufficient,
    choose_clarified_text,
    classify_with_llm,
    force_interpretation,
    merge_clarification,
)
from llm_agent import AgentDecision, LLMError


class FakeClient:
    """Fake LLM-клиент: возвращает заданный текст или бросает ошибку."""

    def __init__(self, answer: str | Exception) -> None:
        self._answer = answer
        self.calls = 0

    def chat(self, messages: list[dict[str, str]]) -> str:
        self.calls += 1
        if isinstance(self._answer, Exception):
            raise self._answer
        return self._answer


# --------------------------------------------------------------------------- #
# Склейка
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("base", "answer", "expected"),
    [
        ("топ 5 товаров по продажам", "2024 год", "топ 5 товаров по продажам 2024 год"),
        ("", "2024 год", "2024 год"),
        ("топ 5 товаров", "", "топ 5 товаров"),
        ("топ 5 товаров", "топ 5 товаров за 2024", "топ 5 товаров за 2024"),
        ("топ 5 товаров за 2024", "топ 5 товаров", "топ 5 товаров за 2024"),
    ],
)
def test_merge_clarification(base: str, answer: str, expected: str) -> None:
    """Склейка сохраняет обе части и не дублирует уже полную формулировку."""
    assert merge_clarification(base, answer) == expected


# --------------------------------------------------------------------------- #
# Правила: полный вопрос vs фрагмент
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("Сколько мы продали за 2024 год?", True),
        ("Как менялись продажи по месяцам в 2023 году?", True),
        ("2024 год", False),
        ("2023 и 2024", False),
        ("первое полугодие 2024", False),
        ("", False),
    ],
)
def test_answer_is_self_sufficient(answer: str, expected: bool) -> None:
    """Полный вопрос распознаётся планировщиком, фрагмент периода — нет."""
    assert answer_is_self_sufficient(answer) is expected


# --------------------------------------------------------------------------- #
# Решение: что показать
# --------------------------------------------------------------------------- #
def test_self_sufficient_answer_keeps_only_second_part() -> None:
    """Полный вопрос не склеивается с исходным — показываем только его."""
    result = choose_clarified_text("продажи", "Сколько мы продали за 2024 год?")
    assert result.text == "Сколько мы продали за 2024 год?"
    assert result.source == SOURCE_ANSWER


def test_fragment_answer_is_merged_with_base() -> None:
    """Фрагмент периода дополняет исходный вопрос, а не заменяет его."""
    result = choose_clarified_text("топ 5 товаров по продажам", "2024 год")
    assert result.text == "топ 5 товаров по продажам 2024 год"
    assert result.source == SOURCE_MERGED


def test_empty_answer_keeps_base() -> None:
    """Пустое уточнение не обнуляет исходный вопрос."""
    result = choose_clarified_text("топ 5 товаров по продажам", "")
    assert result.text == "топ 5 товаров по продажам"
    assert result.source == SOURCE_MERGED


def test_no_base_keeps_answer() -> None:
    """Если исходного вопроса нет, остаётся только уточнение."""
    result = choose_clarified_text("", "2024 год")
    assert result.text == "2024 год"
    assert result.source == SOURCE_ANSWER


# --------------------------------------------------------------------------- #
# Гибрид: LLM только в спорном случае
# --------------------------------------------------------------------------- #
UNCERTAIN_ANSWER = "Что-нибудь интересное за 2024?"


def test_uncertain_case_without_llm_falls_back_to_merge() -> None:
    """Без LLM спорный случай решается безопасной склейкой."""
    result = choose_clarified_text("продажи", UNCERTAIN_ANSWER, use_llm=False)
    assert result.text == f"продажи {UNCERTAIN_ANSWER}"
    assert result.source == SOURCE_MERGED


def test_llm_says_self_sufficient() -> None:
    """LLM подтвердила самодостаточность — остаётся вторая часть."""
    client = FakeClient('{"self_sufficient": true}')
    result = choose_clarified_text("продажи", UNCERTAIN_ANSWER, use_llm=True, client=client)
    assert result.text == UNCERTAIN_ANSWER
    assert result.source == SOURCE_LLM
    assert client.calls == 1


def test_llm_says_fragment() -> None:
    """LLM считает вторую часть фрагментом — склейка, решение помечено как LLM."""
    client = FakeClient('{"self_sufficient": false}')
    result = choose_clarified_text("продажи", UNCERTAIN_ANSWER, use_llm=True, client=client)
    assert result.text == f"продажи {UNCERTAIN_ANSWER}"
    assert result.source == SOURCE_LLM
    assert client.calls == 1


def test_llm_error_falls_back_to_merge() -> None:
    """Ошибка LLM не ломает приложение: безопасная склейка, без падения."""
    client = FakeClient(LLMError("сеть недоступна"))
    result = choose_clarified_text("продажи", UNCERTAIN_ANSWER, use_llm=True, client=client)
    assert result.text == f"продажи {UNCERTAIN_ANSWER}"
    assert result.source == SOURCE_MERGED


def test_classify_with_llm_parses_wrapped_json() -> None:
    """Флаг достаётся из ответа с пояснениями/код-блоком."""
    client = FakeClient('Вот ответ:\n```json\n{"self_sufficient": true}\n```')
    assert classify_with_llm("продажи", "2024 год?", client=client) is True


def test_classify_with_llm_returns_none_on_garbage() -> None:
    """Неразборчивый ответ — не ошибка, решение остаётся за правилами."""
    client = FakeClient("не знаю")
    assert classify_with_llm("продажи", "2024 год?", client=client) is None


def test_classify_with_llm_without_client_or_settings() -> None:
    """Без клиента и настроек LLM не вызывается вовсе."""
    assert classify_with_llm("продажи", "2024 год?") is None


# --------------------------------------------------------------------------- #
# Принудительная интерпретация (лимит уточнений)
# --------------------------------------------------------------------------- #
class FakePlanner:
    """Fake-планировщик: возвращает заранее заданное решение."""

    def __init__(self, decision: AgentDecision) -> None:
        self._decision = decision
        self.calls = 0

    def plan(self, question: str, data_profile: dict | None = None) -> AgentDecision:
        self.calls += 1
        return self._decision


def test_force_interpretation_returns_valid_decision() -> None:
    """Валидное решение правил принимается как ближайшая интерпретация."""
    valid = AgentDecision(ok=True, intent="top_bottom_products", params={"limit": 5})
    planner = FakePlanner(valid)
    assert force_interpretation("топ-5", planner=planner) is valid
    assert planner.calls == 1


def test_force_interpretation_returns_none_on_clarification() -> None:
    """Если правила тоже просят уточнение, интерпретации нет."""
    clarification = AgentDecision(ok=True, intent="clarification")
    assert force_interpretation("что-нибудь", planner=FakePlanner(clarification)) is None


def test_force_interpretation_collapses_compound_question() -> None:
    """Составной запрос сводится к одному показателю за общий диапазон."""
    decision = force_interpretation(
        "топ-5 по выручке за 2023 и отдельно топ-5 за 2024"
    )
    assert decision is not None
    assert decision.intent == "top_bottom_products"
    assert decision.params["start_date"] == date(2023, 1, 1)
    assert decision.params["end_date"] == date(2024, 12, 31)
    assert decision.params["limit"] == 5
