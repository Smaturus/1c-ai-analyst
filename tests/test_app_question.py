"""Тесты блока вопроса: поле ввода, кнопка очистки, кнопка «Спросить» и Quick Replies.

Форма ``st.form`` заменена обычными виджетами, поэтому поле можно очистить
одной кнопкой без отправки. Тесты проверяют:
* кнопка «✖️» очищает ``st.session_state["question_input"]``;
* «Спросить» отправляет вопрос и строит результат (в офлайн-режиме, без API);
* пустой вопрос даёт предупреждение, а не исключение;
* уточняющий вопрос показывает варианты ответа «таблетками» (``st.pills``) и
  выбор варианта подставляет текст в поле и отправляет вопрос автоматически.
"""

from __future__ import annotations

import json

import pytest
from streamlit.testing.v1 import AppTest

from onec_analyst.llm_agent import INTENT_EXAMPLES, INTENT_NAMES
from tests.paths import APP_FILE

TIMEOUT = 120

# Вопрос, который офлайн-правила честно распознают как неоднозначный.
CLARIFICATION_QUESTION = "Покажи что-нибудь интересное"
SUGGESTIONS = [INTENT_EXAMPLES[name] for name in INTENT_NAMES]
TOTAL_SUGGESTION = INTENT_EXAMPLES["total"]


@pytest.fixture()
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Принудительный офлайн-режим: тесты не ходят в API и не зависят от .env."""
    monkeypatch.setenv("LLM_PROVIDER", "none")
    monkeypatch.delenv("LLM_API_KEY", raising=False)


def _button_with_label(at: AppTest, label: str):
    """Найти кнопку с заданной подписью (порядок кнопок в дереве не гарантирован)."""
    for button in at.button:
        if button.label == label:
            return button
    raise AssertionError(f"кнопка {label!r} не найдена: {[b.label for b in at.button]}")


def _set_question(at: AppTest, question: str) -> None:
    """Записать вопрос в поле ввода.

    Поле живёт внутри ``st.form`` (чтобы Enter отправлял вопрос), а AppTest не
    применяет ``set_value`` к виджетам формы до её отправки — поэтому состояние
    задаётся напрямую. В браузере текст, разумеется, вводится в поле.
    """
    at.session_state["question_input"] = question
    at.run()


def test_question_input_has_clear_button(offline: None) -> None:
    """Рядом с полем вопроса есть кнопка очистки с подсказкой."""
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()

    assert len(at.exception) == 0
    clear = _button_with_label(at, "✖️")
    assert clear.help == "Очистить поле"


def test_question_form_enables_enter(offline: None) -> None:
    """Поле вопроса и кнопка «Спросить» — внутри формы: Enter отправляет вопрос."""
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()

    assert len(at.exception) == 0
    assert len(at.get("form")) == 1
    assert len(at.text_input) == 1
    assert "Спросить" in [button.label for button in at.button]


def test_clear_button_empties_question(offline: None) -> None:
    """Нажатие «✖️» очищает поле ввода и не отправляет вопрос."""
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()
    _set_question(at, "Динамика по месяцам за 2023")

    _button_with_label(at, "✖️").click()
    at.run()

    assert len(at.exception) == 0
    assert at.text_input[0].value == ""
    assert at.session_state["question_input"] == ""


def test_question_survives_without_clear_button(offline: None) -> None:
    """Без нажатия «✖️» введённый текст остаётся в поле до отправки."""
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()
    _set_question(at, "Итог за 2024")

    assert at.text_input[0].value == "Итог за 2024"


def test_submit_button_builds_result(offline: None) -> None:
    """«Спросить» отправляет вопрос и показывает результат в офлайн-режиме."""
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()
    _set_question(at, "Итог за 2024")

    _button_with_label(at, "Спросить").click()
    at.run()

    assert len(at.exception) == 0
    assert "Итог за период" in [subheader.value for subheader in at.subheader]


def test_empty_question_shows_warning(offline: None) -> None:
    """Отправка пустого вопроса — предупреждение, а не трейсбек."""
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()

    _button_with_label(at, "Спросить").click()
    at.run()

    assert len(at.exception) == 0
    assert any("Введите вопрос" in warning.value for warning in at.warning)


def _ask(at: AppTest, question: str) -> None:
    """Задать вопрос через поле ввода и кнопку «Спросить»."""
    _set_question(at, question)
    _button_with_label(at, "Спросить").click()
    at.run()


def test_clarification_renders_pill_suggestions(offline: None) -> None:
    """Неоднозначный вопрос: варианты ответа — кликабельные pills, а не список."""
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()
    _ask(at, CLARIFICATION_QUESTION)

    assert len(at.exception) == 0
    assert any("Нужно уточнение" in info.value for info in at.info)
    assert len(at.pills) == 1
    assert list(at.pills[0].options) == SUGGESTIONS
    # Старый маркированный список «Варианты:» заменён таблетками.
    assert not any("**Варианты:**" in element.value for element in at.markdown)
    assert not any(suggestion in element.value for element in at.markdown for suggestion in SUGGESTIONS)


def test_pill_choice_uses_self_sufficient_answer(offline: None) -> None:
    """Полный вопрос из pills не склеивается с исходным: поле, запрос и история совпадают."""
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()
    _ask(at, CLARIFICATION_QUESTION)

    at.pills[0].select(TOTAL_SUGGESTION).run()

    assert len(at.exception) == 0
    # «Покажи что-нибудь интересное» + полный вопрос → показываем только вторую часть.
    assert at.session_state["question_input"] == TOTAL_SUGGESTION
    assert at.text_input[0].value == TOTAL_SUGGESTION
    assert at.session_state["auto_submit"] is False
    assert at.session_state["clarification_source"] == "answer"
    assert at.session_state["history"][-2]["content"] == TOTAL_SUGGESTION
    assert "Итог за период" in [subheader.value for subheader in at.subheader]


def test_pill_choice_merges_fragment_answer(offline: None) -> None:
    """Фрагмент периода из pills склеивается с исходным вопросом."""
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()
    base = "У каких контрагентов выросли продажи?"
    _ask(at, base)

    assert len(at.pills) == 1
    fragment = at.pills[0].options[0]  # «2023 и 2024»
    at.pills[0].select(fragment).run()

    merged = f"{base} {fragment}"
    assert len(at.exception) == 0
    assert at.session_state["question_input"] == merged
    assert at.text_input[0].value == merged
    assert at.session_state["auto_submit"] is False
    assert at.session_state["clarification_source"] == "merged"
    assert at.session_state["history"][-2]["content"] == merged
    assert "Динамика контрагентов" in [subheader.value for subheader in at.subheader]


def test_clarification_rounds_increment_and_reset(offline: None) -> None:
    """Счётчик уточнений растёт при уточнении и сбрасывается после ответа."""
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()
    _ask(at, CLARIFICATION_QUESTION)

    assert at.session_state["clarification_rounds"] == 1

    at.pills[0].select(TOTAL_SUGGESTION).run()
    assert at.session_state["clarification_rounds"] == 0


def test_negative_revenue_question_is_not_antipot(offline: None) -> None:
    """Вопрос про отрицательную выручку даёт возвраты, а не «худшие товары»."""
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()
    _ask(at, "покажи товары с отрицательной выручкой за 2024")

    assert len(at.exception) == 0
    assert "Отрицательная выручка / возвраты" in [
        subheader.value for subheader in at.subheader
    ]
    assert any(
        "Общая сумма возвратов" in element.value for element in at.markdown
    )
    table = at.dataframe[0].value
    assert (table["revenue"] < 0).all()
    assert "rows_count" in table.columns


def test_technical_details_json_is_copyable_code(offline: None) -> None:
    """JSON-DSL выводится в st.code (встроенная кнопка копирования), а не st.json."""
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()
    _ask(at, "Итог за 2024")

    payloads: list[object] = []
    for element in at.code:
        try:
            payloads.append(json.loads(element.value))
        except (TypeError, ValueError):
            continue
    assert any(
        isinstance(payload, dict) and payload.get("intent") == "total"
        for payload in payloads
    ), payloads
    assert any(
        "Кнопка копирования JSON" in caption.value for caption in at.caption
    )


def test_clarification_limit_stops_loop(offline: None) -> None:
    """После лимита уточнений новый круг не показывается: цикл гарантированно прерван."""
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()
    at.session_state["clarification_rounds"] = 2  # лимит уже исчерпан

    _ask(at, CLARIFICATION_QUESTION)

    assert len(at.exception) == 0
    assert any(
        "однозначно разобрать вопрос" in warning.value for warning in at.warning
    )
    assert len(at.pills) == 0  # нового круга уточнений нет
    assert at.session_state["clarification_rounds"] == 0
    # Правила тоже не смогли выбрать интент — показываем понятную ошибку и примеры.
    assert any("Не удалось разобрать вопрос" in error.value for error in at.error)
