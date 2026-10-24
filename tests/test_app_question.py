"""Тесты блока вопроса: поле ввода, кнопка очистки и кнопка «Спросить».

Форма ``st.form`` заменена обычными виджетами, поэтому поле можно очистить
одной кнопкой без отправки. Тесты проверяют:
* кнопка «✖️» очищает ``st.session_state["question_input"]``;
* «Спросить» отправляет вопрос и строит результат (в офлайн-режиме, без API);
* пустой вопрос даёт предупреждение, а не исключение.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

APP_FILE = Path(__file__).resolve().parents[1] / "app.py"
TIMEOUT = 120


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


def test_question_input_has_clear_button(offline: None) -> None:
    """Рядом с полем вопроса есть кнопка очистки с подсказкой."""
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()

    assert len(at.exception) == 0
    clear = _button_with_label(at, "✖️")
    assert clear.help == "Очистить поле"


def test_clear_button_empties_question(offline: None) -> None:
    """Нажатие «✖️» очищает поле ввода без перезагрузки формы."""
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()
    at.text_input[0].set_value("Динамика по месяцам за 2023").run()

    _button_with_label(at, "✖️").click()
    at.run()

    assert len(at.exception) == 0
    assert at.text_input[0].value == ""
    assert at.session_state["question_input"] == ""


def test_question_survives_without_clear_button(offline: None) -> None:
    """Без нажатия «✖️» введённый текст остаётся в поле до отправки."""
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()
    at.text_input[0].set_value("Итог за 2024").run()

    assert at.text_input[0].value == "Итог за 2024"


def test_submit_button_builds_result(offline: None) -> None:
    """«Спросить» отправляет вопрос и показывает результат в офлайн-режиме."""
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()
    at.text_input[0].set_value("Итог за 2024").run()

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
