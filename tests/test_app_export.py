"""Кнопка «Выгрузить» в UI: рядом с «Спросить», без расчёта и без дублей."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from tests.paths import APP_FILE

TIMEOUT = 120


@pytest.fixture()
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Офлайн-режим: правила вместо API, журнал — в изолированной БД."""
    monkeypatch.setenv("LLM_PROVIDER", "none")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("NOTION_API_KEY", raising=False)
    monkeypatch.delenv("NOTION_DATABASE_ID", raising=False)


def _button(at: AppTest, label: str):
    for button in at.button:
        if button.label == label:
            return button
    raise AssertionError(f"кнопка {label!r} не найдена: {[b.label for b in at.button]}")


def _rows(path: Path) -> list[sqlite3.Row]:
    if not path.exists():
        return []
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        try:
            return connection.execute("SELECT * FROM sessions ORDER BY id").fetchall()
        except sqlite3.OperationalError:
            return []
    finally:
        connection.close()


def test_export_button_sits_next_to_ask(offline: None) -> None:
    """«Выгрузить» есть на странице и идёт сразу после «Спросить»."""
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()

    labels = [button.label for button in at.button]
    assert "Выгрузить" in labels
    assert labels.index("Выгрузить") == labels.index("Спросить") + 1
    export = _button(at, "Выгрузить")
    assert "активного диалога" in (export.help or "")


def test_export_without_configuration_shows_warning(offline: None) -> None:
    """Без NOTION_* кнопка предупреждает, а не роняет страницу."""
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()
    _button(at, "Выгрузить").click()
    at.run()

    assert len(at.exception) == 0
    assert any("не настроена" in warning.value for warning in at.warning)


def test_export_uses_active_chat_and_reports_success(
    offline: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Кнопка выгружает активный диалог и показывает счётчики."""
    calls: list[str] = []

    def fake_export(chat_id: str) -> dict:
        calls.append(chat_id)
        return {"pending": 2, "exported": 2, "failed": 0}

    monkeypatch.setattr("onec_analyst.notion_export.export_chat", fake_export)
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()
    _button(at, "Выгрузить").click()
    at.run()

    assert len(at.exception) == 0
    assert len(calls) == 1 and calls[0]
    assert any("2 из 2" in success.value for success in at.success)


def test_second_export_reports_nothing_new(
    offline: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Повторная выгрузка без новых сессий пишет «всё уже выгружено»."""
    monkeypatch.setattr(
        "onec_analyst.notion_export.export_chat",
        lambda chat_id: {"pending": 0, "exported": 0, "failed": 0},
    )
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()
    _button(at, "Выгрузить").click()
    at.run()

    assert len(at.exception) == 0
    assert any("уже выгружено" in info.value for info in at.info)


def test_export_failure_does_not_break_page(
    offline: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сетевая ошибка Notion — сообщение об ошибке, страница жива."""

    def boom(chat_id: str) -> dict:
        raise RuntimeError("network down")

    monkeypatch.setattr("onec_analyst.notion_export.export_chat", boom)
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()
    _button(at, "Выгрузить").click()
    at.run()

    assert len(at.exception) == 0
    assert any("network down" in error.value for error in at.error)


def test_export_does_not_submit_question(
    offline: None, session_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Клик «Выгрузить» не запускает расчёт: новых строк в журнале нет."""
    monkeypatch.setattr(
        "onec_analyst.notion_export.export_chat",
        lambda chat_id: {"pending": 0, "exported": 0, "failed": 0},
    )
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()
    at.session_state["question_input"] = "Итог за 2024"
    at.run()
    _button(at, "Выгрузить").click()
    at.run()

    assert len(at.exception) == 0
    assert _rows(session_db_path) == []
    assert "Итог за период" not in [subheader.value for subheader in at.subheader]


def _ask(at: AppTest, question: str) -> None:
    """Задать вопрос через поле и кнопку «Спросить»."""
    at.session_state["question_input"] = question
    at.run()
    _button(at, "Спросить").click()
    at.run()


def test_result_survives_export_click(
    offline: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """После «Выгрузить» таблица результата остаётся на экране."""
    monkeypatch.setattr(
        "onec_analyst.notion_export.export_chat",
        lambda chat_id: {"pending": 0, "exported": 0, "failed": 0},
    )
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()
    _ask(at, "Итог за 2024")
    assert "Итог за период" in [subheader.value for subheader in at.subheader]

    _button(at, "Выгрузить").click()
    at.run()

    assert len(at.exception) == 0
    assert "Итог за период" in [subheader.value for subheader in at.subheader]


def test_result_survives_widget_rerun(offline: None) -> None:
    """Любое взаимодействие (смена виджета) не стирает последний ответ."""
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()
    _ask(at, "Итог за 2024")
    assert "Итог за период" in [subheader.value for subheader in at.subheader]

    at.run()

    assert "Итог за период" in [subheader.value for subheader in at.subheader]
