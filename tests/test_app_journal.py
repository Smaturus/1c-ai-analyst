"""Журнал сессий на уровне UI: одна строка на запрос, ответ не зависит от записи."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from onec_analyst import session_log
from onec_analyst.session_log import SessionLog
from tests.paths import APP_FILE

TIMEOUT = 120


@pytest.fixture()
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Офлайн-режим: правила вместо API, журнал пишется в изолированную БД."""
    monkeypatch.setenv("LLM_PROVIDER", "none")
    monkeypatch.delenv("LLM_API_KEY", raising=False)


def _ask(at: AppTest, question: str) -> None:
    """Задать вопрос через поле ввода и кнопку «Спросить»."""
    at.session_state["question_input"] = question
    at.run()
    for button in at.button:
        if button.label == "Спросить":
            button.click()
            break
    else:  # pragma: no cover - защита от переименования кнопки
        raise AssertionError(f"кнопка «Спросить» не найдена: {[b.label for b in at.button]}")
    at.run()


def _rows(path: Path) -> list[sqlite3.Row]:
    """Прочитать строки журнала; отсутствие файла/таблицы — пустой список."""
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


def test_successful_question_writes_one_row(
    offline: None, session_db_path: Path
) -> None:
    """Успешный расчёт: ровно одна строка, verdict=1, dsl и latency заполнены."""
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()
    _ask(at, "Итог за 2024")

    assert len(at.exception) == 0
    rows = _rows(session_db_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "ok"
    assert row["verdict"] == 1
    assert row["question"] == "Итог за 2024"
    assert row["dsl"] and "total" in row["dsl"]
    assert row["latency_sec"] is not None and row["latency_sec"] >= 0
    assert row["session_id"]


def test_each_request_gets_its_own_session_id(
    offline: None, session_db_path: Path
) -> None:
    """Два вопроса — две строки с разными ``session_id``."""
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()
    _ask(at, "Итог за 2024")
    _ask(at, "Топ-5 товаров за 2024")

    rows = _rows(session_db_path)
    assert len(rows) == 2
    assert rows[0]["session_id"] != rows[1]["session_id"]


def test_successful_question_stores_result_summary(
    offline: None, session_db_path: Path
) -> None:
    """В журнал попадает сводка результата, а не только метаданные сессии."""
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()
    _ask(at, "Итог за 2024")

    row = _rows(session_db_path)[0]
    summary = json.loads(row["summary"])
    assert summary["intent"] == "total"
    assert summary["text"]
    assert summary["values"], "значения результата должны сохраняться для отчёта"
    assert summary["table"]["rows"], "снимок таблицы результата должен попадать в журнал"


def test_dialog_chain_links_requests(
    offline: None, session_db_path: Path
) -> None:
    """Два вопроса подряд — один chat_id, ходы 1 и 2, текст ответа сохранён."""
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()
    _ask(at, "Итог за 2024")
    _ask(at, "Топ-5 товаров за 2024")

    rows = _rows(session_db_path)
    assert len(rows) == 2
    assert rows[0]["chat_id"] == rows[1]["chat_id"]
    assert [row["turn"] for row in rows] == [1, 2]
    assert rows[0]["answer"] and "2024" in rows[0]["answer"]
    assert rows[1]["answer"]


def test_clarification_is_logged_without_verdict(
    offline: None, session_db_path: Path
) -> None:
    """Уточнение — отдельный статус и ``verdict=0`` (расчёта не было)."""
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()
    _ask(at, "Покажи что-нибудь интересное")

    assert len(at.exception) == 0
    row = _rows(session_db_path)[0]
    assert row["status"] == "clarify"
    assert row["verdict"] == 0


def test_empty_question_is_not_logged(offline: None, session_db_path: Path) -> None:
    """Пустая отправка не создаёт строку: сессии не было."""
    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()
    for button in at.button:
        if button.label == "Спросить":
            button.click()
            break
    at.run()

    assert any("Введите вопрос" in warning.value for warning in at.warning)
    assert _rows(session_db_path) == []


def test_journal_failure_does_not_break_answer(
    offline: None,
    session_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ошибка записи журнала: ответ всё равно строится, исключений нет."""

    def boom(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(SessionLog, "_write", staticmethod(boom))
    monkeypatch.setattr(session_log.time, "sleep", lambda _seconds: None)

    at = AppTest.from_file(APP_FILE, default_timeout=TIMEOUT).run()
    _ask(at, "Итог за 2024")

    assert len(at.exception) == 0
    assert "Итог за период" in [subheader.value for subheader in at.subheader]
    assert not session_db_path.exists()
