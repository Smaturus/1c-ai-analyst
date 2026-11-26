"""Журнал сессий: схема, маппинг статусов, fail-safe и защита от попадания в git."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from onec_analyst import session_log
from onec_analyst.session_log import (
    JOURNAL_STATUSES,
    MAX_ANSWER_CHARS,
    MAX_RAW_CHARS,
    RAW_ENV_VAR,
    SessionLog,
    SessionRecord,
    journal_status,
    mark_exported,
    pending_records,
    record_request,
    summary_to_json,
)
from tests.paths import REPO_ROOT

#: Поля, перечисленные в ТЗ; остальные колонки — служебные.
REQUIRED_COLUMNS = (
    "ts",
    "session_id",
    "question",
    "provider",
    "model",
    "dsl",
    "status",
    "verdict",
    "latency_sec",
    "prompt_tokens",
    "output_tokens",
    "degraded",
)


def _columns(path: Path) -> list[str]:
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute("PRAGMA table_info(sessions)").fetchall()
    finally:
        connection.close()
    return [row[1] for row in rows]


def _rows(path: Path) -> list[sqlite3.Row]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute("SELECT * FROM sessions ORDER BY id").fetchall()
    finally:
        connection.close()


def test_schema_has_all_required_columns(session_db_path: Path) -> None:
    """Таблица создаётся при первой записи и содержит все поля ТЗ."""
    assert SessionLog(session_db_path).record(SessionRecord(session_id="s1", question="q"))
    assert session_db_path.exists()
    columns = _columns(session_db_path)
    for name in REQUIRED_COLUMNS:
        assert name in columns, f"нет колонки {name}: {columns}"


def test_one_row_per_record(session_db_path: Path) -> None:
    """Одна запись журнала = одна строка (и повторные записи не схлопываются)."""
    log = SessionLog(session_db_path)
    assert log.record(SessionRecord(session_id="a", question="первый"))
    assert log.record(SessionRecord(session_id="b", question="второй"))
    rows = _rows(session_db_path)
    assert [row["session_id"] for row in rows] == ["a", "b"]


def test_record_persists_values_and_json_dsl(session_db_path: Path) -> None:
    """Значения доезжают как есть, DSL — валидный JSON-текст."""
    dsl = {"intent": "top_products", "params": {"limit": 5}}
    assert SessionLog(session_db_path).record(
        SessionRecord(
            session_id="sess-1",
            question="топ-5 товаров за 2024",
            provider="deepseek",
            model="deepseek-v4-flash",
            dsl=dsl,
            status="ok",
            verdict=True,
            latency_sec=1.25,
            degraded=False,
        )
    )
    row = _rows(session_db_path)[0]
    assert row["question"] == "топ-5 товаров за 2024"
    assert row["provider"] == "deepseek"
    assert row["model"] == "deepseek-v4-flash"
    assert json.loads(row["dsl"]) == dsl
    assert row["status"] == "ok"
    assert row["verdict"] == 1
    assert row["latency_sec"] == pytest.approx(1.25)
    assert row["prompt_tokens"] is None
    assert row["output_tokens"] is None
    assert row["degraded"] == 0
    assert row["exported_at"] is None


def test_fallback_is_ok_with_degraded_flag(session_db_path: Path) -> None:
    """``fallback`` → ``ok`` + ``degraded=1`` (select Notion остаётся четырёхзначным)."""
    assert SessionLog(session_db_path).record(
        SessionRecord(session_id="s", question="q", status="fallback", verdict=False)
    )
    row = _rows(session_db_path)[0]
    assert row["status"] == "ok"
    assert row["degraded"] == 1


def test_clarification_limit_ok_with_explicit_degraded(session_db_path: Path) -> None:
    """Лимит уточнений: ``status=ok`` + явный ``degraded`` (ADR 0007)."""
    from onec_analyst.session_log import record_request

    assert record_request(
        session_id="s",
        question="топ товаров",
        provider=None,
        model=None,
        dsl={"intent": "top_bottom_products"},
        status="ok",
        verdict=True,
        degraded=True,
    )
    row = _rows(session_db_path)[0]
    assert row["status"] == "ok"
    assert row["degraded"] == 1


@pytest.mark.parametrize("status", JOURNAL_STATUSES)
def test_known_statuses_survive(status: str) -> None:
    """Четыре статуса ТЗ не искажаются маппингом."""
    assert journal_status(status) == status


def test_unknown_status_becomes_error() -> None:
    """Неизвестный статус не выдаётся за успех."""
    assert journal_status("weird") == "error"
    assert journal_status(None) == "error"


def test_verdict_comes_from_caller_not_from_status(session_db_path: Path) -> None:
    """``verdict`` — отдельный факт (был ли расчёт), а не синоним ``status``."""
    log = SessionLog(session_db_path)
    assert log.record(SessionRecord(session_id="a", status="ok", verdict=False))
    assert log.record(SessionRecord(session_id="b", status="ok", verdict=True))
    assert [row["verdict"] for row in _rows(session_db_path)] == [0, 1]


def test_record_is_fail_safe_on_sqlite_error(
    session_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ошибка записи не выходит наружу и возвращает ``False``."""

    def boom(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(SessionLog, "_write", staticmethod(boom))
    monkeypatch.setattr(session_log.time, "sleep", lambda _seconds: None)
    assert SessionLog(session_db_path).record(SessionRecord(session_id="s")) is False


def test_record_is_fail_safe_on_unwritable_path(tmp_path: Path) -> None:
    """Каталог вместо файла БД — тоже не повод падать."""
    directory = tmp_path / "not_a_db"
    directory.mkdir()
    assert SessionLog(directory).record(SessionRecord(session_id="s")) is False


def test_record_rejects_non_record(session_db_path: Path) -> None:
    """Случайный dict вместо записи игнорируется, а не роняет запрос."""
    assert SessionLog(session_db_path).record({"session_id": "s"}) is False  # type: ignore[arg-type]


def test_pending_missing_db_is_empty(tmp_path: Path) -> None:
    """На чистой установке журнала ещё нет — экспортёр получает пустой список."""
    assert pending_records(tmp_path / "nope.db") == []


def test_pending_then_mark_exported(session_db_path: Path) -> None:
    """Невыгруженные читаются по порядку, после отметки — исчезают из выборки."""
    log = SessionLog(session_db_path)
    assert log.record(SessionRecord(session_id="a", question="q1"))
    assert log.record(SessionRecord(session_id="b", question="q2"))

    pending = pending_records(session_db_path)
    assert [row["session_id"] for row in pending] == ["a", "b"]

    mark_exported(session_db_path, pending[0]["id"], "page-1")
    rest = pending_records(session_db_path)
    assert [row["session_id"] for row in rest] == ["b"]
    assert rest[0]["question"] == "q2"


def test_pending_limit_is_applied(session_db_path: Path) -> None:
    """Порция ограничивается ``limit`` — экспорт не тянет всю базу в память."""
    log = SessionLog(session_db_path)
    for index in range(3):
        assert log.record(SessionRecord(session_id=f"s{index}"))
    assert len(pending_records(session_db_path, limit=2)) == 2


@pytest.mark.skipif(shutil.which("git") is None, reason="git недоступен")
def test_session_db_is_not_tracked_by_git() -> None:
    """Тест-страж: журнал не должен оказаться в индексе git.

    Правила ``.gitignore`` одни, но ошибка «``git add -A`` после первого запуска»
    стоит дорого: в журнале текст вопросов. Проверяем и индекс, и игнор.
    """
    tracked = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "--", "db"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert tracked.stdout.strip() == "", f"db/ уже в индексе git: {tracked.stdout}"

    ignored = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "check-ignore", "-q", "db/sessions.db"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert ignored.returncode == 0, "db/sessions.db не покрыт .gitignore/.git/info/exclude"


# --------------------------------------------------------------------------- #
# Сводка результата: хранение, сериализация, миграция старой базы
# --------------------------------------------------------------------------- #
SUMMARY = {
    "text": "За 2024 год: Лидер «P1» — 1 000,00 ₽.",
    "intent": "top_bottom_products",
    "values": [
        {"item": "P1", "metric": "revenue", "period": "2024", "value": 1000.0},
        {"item": "P2", "metric": "revenue", "period": "2024", "value": 500.0},
    ],
    "periods": ["2024"],
    "entities": ["P1", "P2"],
}


def test_summary_is_stored_and_read(session_db_path: Path) -> None:
    """Сводка результата сохраняется в журнал как JSON и возвращается в pending."""
    assert SessionLog(session_db_path).record(
        SessionRecord(session_id="s", summary=SUMMARY, verdict=True)
    )
    row = pending_records(session_db_path)[0]
    assert json.loads(row["summary"]) == SUMMARY


def test_summary_to_json_from_payload_object() -> None:
    """``SummaryPayload``-подобный объект превращается в JSON-безопасный dict."""

    class Payload:
        text = SUMMARY["text"]
        intent = SUMMARY["intent"]
        values = tuple(SUMMARY["values"])
        periods = ("2024",)
        entities = ("P1", "P2")

    assert summary_to_json(Payload()) == SUMMARY
    assert summary_to_json(None) is None


def test_summary_to_json_tolerates_broken_payload() -> None:
    """Кривой payload не роняет журнал: возвращается ``None``."""

    class Broken:
        text = "x"

        @property
        def values(self):
            raise ValueError("bad")

    assert summary_to_json(Broken()) is None


def test_old_database_is_migrated_with_summary_column(session_db_path: Path) -> None:
    """База первой версии (без ``summary``) получает колонку без потери строк."""
    old_schema = """
    CREATE TABLE sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        session_id TEXT NOT NULL,
        question TEXT NOT NULL DEFAULT '',
        provider TEXT,
        model TEXT,
        dsl TEXT,
        status TEXT NOT NULL,
        verdict INTEGER NOT NULL DEFAULT 0,
        latency_sec REAL,
        prompt_tokens INTEGER,
        output_tokens INTEGER,
        degraded INTEGER NOT NULL DEFAULT 0,
        notion_page_id TEXT,
        exported_at TEXT
    )
    """
    connection = sqlite3.connect(session_db_path)
    try:
        connection.execute(old_schema)
        connection.execute(
            "INSERT INTO sessions (ts, session_id, status) VALUES ('2026-01-01T00:00:00+03:00', 'old', 'ok')"
        )
        connection.commit()
    finally:
        connection.close()
    assert "summary" not in _columns(session_db_path)

    assert SessionLog(session_db_path).record(
        SessionRecord(session_id="new", summary=SUMMARY, verdict=True)
    )

    assert "summary" in _columns(session_db_path)
    rows = _rows(session_db_path)
    assert [row["session_id"] for row in rows] == ["old", "new"]
    assert rows[0]["summary"] is None
    assert json.loads(rows[1]["summary"]) == SUMMARY


# --------------------------------------------------------------------------- #
# Сырой ответ модели: только по флагу, никогда в выгрузку
# --------------------------------------------------------------------------- #
RAW = '{"name": "top_bottom_products", "arguments": {"limit": 5}}'


def test_raw_response_is_not_stored_by_default(
    session_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Без флага журнал остаётся метаданными: сырой ответ не пишется."""
    monkeypatch.delenv(RAW_ENV_VAR, raising=False)
    assert SessionLog(session_db_path).record(
        SessionRecord(session_id="s", raw_response=RAW)
    )
    assert _rows(session_db_path)[0]["raw_response"] is None


def test_raw_response_is_stored_when_enabled(
    session_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ONEC_SESSION_LOG_RAW=1`` включает диагностическое поле."""
    monkeypatch.setenv(RAW_ENV_VAR, "1")
    assert SessionLog(session_db_path).record(
        SessionRecord(session_id="s", raw_response=RAW)
    )
    assert _rows(session_db_path)[0]["raw_response"] == RAW


def test_raw_response_is_clipped(
    session_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Гигантский ответ обрезается: база не растёт без предела."""
    monkeypatch.setenv(RAW_ENV_VAR, "1")
    long_raw = "x" * (MAX_RAW_CHARS + 100)
    assert SessionLog(session_db_path).record(
        SessionRecord(session_id="s", raw_response=long_raw)
    )
    stored = _rows(session_db_path)[0]["raw_response"]
    assert len(stored) == MAX_RAW_CHARS + 1
    assert stored.endswith("…")


def test_export_does_not_receive_raw_response(
    session_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """В выборку для Notion сырой ответ не попадает даже при включённом флаге."""
    monkeypatch.setenv(RAW_ENV_VAR, "1")
    assert SessionLog(session_db_path).record(
        SessionRecord(session_id="s", raw_response=RAW)
    )
    row = pending_records(session_db_path)[0]
    assert "raw_response" not in row


def test_old_database_gets_raw_response_column(session_db_path: Path) -> None:
    """Миграция добавляет и колонку ``raw_response`` (база первой версии)."""
    connection = sqlite3.connect(session_db_path)
    try:
        connection.execute(
            """
            CREATE TABLE sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                session_id TEXT NOT NULL,
                question TEXT NOT NULL DEFAULT '',
                provider TEXT,
                model TEXT,
                dsl TEXT,
                status TEXT NOT NULL,
                verdict INTEGER NOT NULL DEFAULT 0,
                latency_sec REAL,
                prompt_tokens INTEGER,
                output_tokens INTEGER,
                degraded INTEGER NOT NULL DEFAULT 0,
                notion_page_id TEXT,
                exported_at TEXT
            )
            """
        )
        connection.commit()
    finally:
        connection.close()

    assert SessionLog(session_db_path).record(SessionRecord(session_id="s"))
    assert "raw_response" in _columns(session_db_path)


# --------------------------------------------------------------------------- #
# История диалога: chat_id, номер хода, полный текст ответа
# --------------------------------------------------------------------------- #
def test_dialog_fields_are_stored(session_db_path: Path) -> None:
    """В журнале есть связка диалога и полный текст ответа."""
    assert SessionLog(session_db_path).record(
        SessionRecord(
            session_id="s1",
            chat_id="chat-1",
            turn=1,
            answer="За 2024 год лидер — P1.",
        )
    )
    row = _rows(session_db_path)[0]
    assert row["chat_id"] == "chat-1"
    assert row["turn"] == 1
    assert row["answer"] == "За 2024 год лидер — P1."


def test_dialog_chain_keeps_order(session_db_path: Path) -> None:
    """Один диалог — общий chat_id и возрастающие номера ходов."""
    log = SessionLog(session_db_path)
    for turn in (1, 2, 3):
        assert log.record(
            SessionRecord(session_id=f"s{turn}", chat_id="chat-1", turn=turn)
        )
    rows = _rows(session_db_path)
    assert [row["chat_id"] for row in rows] == ["chat-1"] * 3
    assert [row["turn"] for row in rows] == [1, 2, 3]


def test_answer_is_clipped(session_db_path: Path) -> None:
    """Слишком длинный ответ обрезается по лимиту."""
    long_answer = "y" * (MAX_ANSWER_CHARS + 50)
    assert SessionLog(session_db_path).record(
        SessionRecord(session_id="s", answer=long_answer)
    )
    stored = _rows(session_db_path)[0]["answer"]
    assert len(stored) == MAX_ANSWER_CHARS + 1
    assert stored.endswith("…")


def test_empty_answer_is_null(session_db_path: Path) -> None:
    """Пустой ответ не хранится как пустая строка."""
    assert SessionLog(session_db_path).record(
        SessionRecord(session_id="s", answer="  ")
    )
    assert _rows(session_db_path)[0]["answer"] is None


def test_export_does_not_receive_dialog_history(session_db_path: Path) -> None:
    """В Notion не уходят ни chat_id/turn, ни полный текст ответа."""
    assert SessionLog(session_db_path).record(
        SessionRecord(
            session_id="s",
            chat_id="chat-1",
            turn=2,
            answer="полный ответ",
        )
    )
    row = pending_records(session_db_path)[0]
    for key in ("chat_id", "turn", "answer", "raw_response"):
        assert key not in row


def test_old_database_gets_dialog_columns(session_db_path: Path) -> None:
    """Миграция добавляет chat_id/turn/answer к базе первой версии."""
    connection = sqlite3.connect(session_db_path)
    try:
        connection.execute(
            """
            CREATE TABLE sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                session_id TEXT NOT NULL,
                question TEXT NOT NULL DEFAULT '',
                provider TEXT,
                model TEXT,
                dsl TEXT,
                status TEXT NOT NULL,
                verdict INTEGER NOT NULL DEFAULT 0,
                latency_sec REAL,
                prompt_tokens INTEGER,
                output_tokens INTEGER,
                degraded INTEGER NOT NULL DEFAULT 0,
                notion_page_id TEXT,
                exported_at TEXT
            )
            """
        )
        connection.commit()
    finally:
        connection.close()

    assert SessionLog(session_db_path).record(SessionRecord(session_id="s", turn=1))
    columns = _columns(session_db_path)
    for name in ("chat_id", "turn", "answer"):
        assert name in columns


def test_data_source_roundtrip(session_db_path: Path) -> None:
    """Подпись источника пишется как есть; без подписи колонка пустая."""
    assert record_request(
        session_id="demo",
        question="q",
        provider=None,
        model=None,
        dsl=None,
        status="ok",
        verdict=False,
        data_source="демо-данные",
        db_path=session_db_path,
    )
    assert SessionLog(session_db_path).record(
        SessionRecord(session_id="blank", question="q2")
    )
    stored = {row["session_id"]: row["data_source"] for row in _rows(session_db_path)}
    assert stored["demo"] == "демо-данные"
    assert stored["blank"] == ""


def test_legacy_rows_keep_blank_data_source(session_db_path: Path) -> None:
    """Старая база читается, а пустой источник не становится 1С или демо."""
    connection = sqlite3.connect(session_db_path)
    try:
        connection.execute(
            """
            CREATE TABLE sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                session_id TEXT NOT NULL,
                question TEXT NOT NULL DEFAULT '',
                provider TEXT,
                model TEXT,
                dsl TEXT,
                status TEXT NOT NULL,
                verdict INTEGER NOT NULL DEFAULT 0,
                latency_sec REAL,
                prompt_tokens INTEGER,
                output_tokens INTEGER,
                degraded INTEGER NOT NULL DEFAULT 0,
                notion_page_id TEXT,
                exported_at TEXT
            )
            """
        )
        connection.execute(
            "INSERT INTO sessions (ts, session_id, question, status) "
            "VALUES ('2026-01-01T00:00:00+03:00', 'old', 'секрет', 'ok')"
        )
        connection.commit()
    finally:
        connection.close()

    assert "data_source" not in _columns(session_db_path)
    legacy = pending_records(session_db_path)[0]
    assert legacy["session_id"] == "old"
    assert legacy["question"] == "секрет"
    assert legacy["data_source"] == ""

    assert SessionLog(session_db_path).record(
        SessionRecord(session_id="new", question="q", data_source="1C")
    )
    assert "data_source" in _columns(session_db_path)
    stored = {row["session_id"]: row["data_source"] for row in _rows(session_db_path)}
    assert stored["old"] == ""
    assert stored["new"] == "1C"


def test_pending_filter_by_chat(session_db_path: Path) -> None:
    """Выборка для кнопки «Выгрузить» ограничена одним диалогом."""
    log = SessionLog(session_db_path)
    for chat_id, turn in (("chat-a", 1), ("chat-a", 2), ("chat-b", 1)):
        assert log.record(
            SessionRecord(session_id=f"{chat_id}-{turn}", chat_id=chat_id, turn=turn)
        )

    active = pending_records(session_db_path, chat_id="chat-a")
    assert [row["session_id"] for row in active] == ["chat-a-1", "chat-a-2"]
    assert len(pending_records(session_db_path)) == 3
    assert pending_records(session_db_path, chat_id="chat-c") == []
