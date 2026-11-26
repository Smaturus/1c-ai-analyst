"""Журнал аналитических сессий в SQLite: одна строка — один запрос пользователя.

Зачем нужен отдельный журнал, если уже есть ``.mcp_state/audit.jsonl``:

* аудит MCP пишет **шаги** графа и не знает про вопрос, вердикт и длительность
  ответа целиком — по нему нельзя посчитать «сколько сессий закончилось
  результатом», а по этому журналу можно;
* SQLite позволяет строить выборки «новые записи с прошлого экспорта» и
  отмечать выгруженные страницы Notion без перечитывания всего файла;
* оба журнала независимы: аудит остаётся как был, новый пишется параллельно.

Главное свойство — **fail-safe**: ошибка записи не прерывает ответ
пользователю. :meth:`SessionLog.record` ловит любые исключения, пишет
предупреждение в лог приложения и возвращает ``False``; вызывающий код не
обязан ничего проверять.

БД лежит в ``db/sessions.db`` (каталог в ``.gitignore`` и ``.git/info/exclude``,
потому что в строке хранится текст вопроса). Путь переопределяется переменной
``ONEC_SESSION_DB`` — этим пользуются тесты.

Значения ``prompt_tokens``/``output_tokens`` пишутся как ``NULL``: контракт
LLM-клиента (:class:`onec_analyst.llm_agent.CompletionResult`) не содержит
usage, а расширять его ради метрики — менять ядро. Поле в схеме оставлено,
чтобы появление токенов не требовало миграции.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from onec_analyst.paths import find_repo_root

__all__ = [
    "DB_ENV_VAR",
    "DEFAULT_DB_FILE",
    "JOURNAL_STATUSES",
    "MAX_ANSWER_CHARS",
    "MAX_RAW_CHARS",
    "RAW_ENV_VAR",
    "SessionLog",
    "SessionRecord",
    "clip_answer",
    "clip_raw_response",
    "default_db_path",
    "exported_records",
    "is_degraded",
    "journal_status",
    "mark_exported",
    "pending_records",
    "raw_logging_enabled",
    "record_request",
    "resolve_db_path",
    "result_table",
    "summary_to_json",
]

logger = logging.getLogger("onec_analyst.session_log")

#: Переменная окружения для пути к журналу (нужна тестам и развёртываниям).
DB_ENV_VAR = "ONEC_SESSION_DB"

#: Флаг «писать сырой ответ модели» (по умолчанию выключен: это диагностика).
RAW_ENV_VAR = "ONEC_SESSION_LOG_RAW"

#: Предел длины сырого ответа: защита БД от гигантских JSON от модели.
MAX_RAW_CHARS = 20000

#: Предел длины полного текста ответа (сводка обычно ≤ 500 символов).
MAX_ANSWER_CHARS = 20000

#: Путь по умолчанию относительно корня проекта.
DEFAULT_DB_FILE = Path("db") / "sessions.db"

#: Статусы, которые понимает журнал и Notion-select (``fallback`` свёрнут в ``ok``).
JOURNAL_STATUSES = ("ok", "clarify", "refuse", "error")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    session_id TEXT NOT NULL,
    question TEXT NOT NULL DEFAULT '',
    provider TEXT,
    model TEXT,
    dsl TEXT,
    summary TEXT,
    raw_response TEXT,
    chat_id TEXT,
    turn INTEGER,
    answer TEXT,
    status TEXT NOT NULL,
    verdict INTEGER NOT NULL DEFAULT 0,
    latency_sec REAL,
    prompt_tokens INTEGER,
    output_tokens INTEGER,
    degraded INTEGER NOT NULL DEFAULT 0,
    notion_page_id TEXT,
    exported_at TEXT,
    data_source TEXT NOT NULL DEFAULT ''
)
"""

_INDEX = "CREATE INDEX IF NOT EXISTS idx_sessions_export ON sessions(exported_at, id)"


def default_db_path() -> Path:
    """Путь журнала по умолчанию: ``db/sessions.db`` в корне проекта."""
    return find_repo_root() / DEFAULT_DB_FILE


def resolve_db_path() -> Path:
    """Путь журнала с учётом ``ONEC_SESSION_DB`` (пустое значение — по умолчанию)."""
    raw = os.environ.get(DB_ENV_VAR, "").strip()
    return Path(raw) if raw else default_db_path()


def journal_status(status: Any) -> str:
    """Привести статус решения к четырём значениям журнала.

    ``fallback`` (ответ на офлайн-правилах вместо LLM) не отдельное состояние
    для читателя отчёта: это успешная сессия с деградацией, поэтому пишется
    ``ok`` + ``degraded=true``. Неизвестное значение — ``error``: молча
    выдавать его за ``ok`` нельзя, иначе метрики врут.
    """
    text = str(status or "").strip()
    if text == "fallback":
        return "ok"
    if text in JOURNAL_STATUSES:
        return text
    return "error"


def is_degraded(status: Any) -> bool:
    """Правда ли, что ответ деградировал (LLM подменена правилами)."""
    return str(status or "").strip() == "fallback"


def raw_logging_enabled() -> bool:
    """Включено ли хранение сырого ответа модели (``ONEC_SESSION_LOG_RAW``).

    По умолчанию выключено: сырой ответ — диагностика, а не продуктовая метрика,
    и он может содержать текст вопроса или фрагменты данных.
    """
    return os.environ.get(RAW_ENV_VAR, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def clip_raw_response(text: Any, limit: int = MAX_RAW_CHARS) -> str | None:
    """Обрезать сырой ответ до ``limit`` символов (``None`` для пустого)."""
    if text is None:
        return None
    value = str(text)
    if not value.strip():
        return None
    return value if len(value) <= limit else value[:limit] + "…"


def clip_answer(text: Any, limit: int = MAX_ANSWER_CHARS) -> str | None:
    """Полный текст ответа: пустой → ``None``, слишком длинный → обрезка."""
    if text is None:
        return None
    value = str(text)
    if not value.strip():
        return None
    return value if len(value) <= limit else value[:limit] + "…"


@dataclass(frozen=True)
class SessionRecord:
    """Одна аналитическая сессия — одна запись журнала.

    Attributes
    ----------
    question:
        Текст вопроса. Хранится **только** локально: наружу (Notion) он не
        уходит, экспортёр подставляет плейсхолдер.
    dsl:
        JSON-безопасный DSL, сериализуется в текст при записи.
    summary:
        Сводка результата (``text``, ``intent``, ``values``, ``periods``,
        ``entities``, ``table``) в JSON-безопасном виде. Нужна, чтобы отчёт в
        Notion показывал, что именно посчитано, а не только метаданные сессии.
    raw_response:
        Сырой ответ модели — **только при** ``ONEC_SESSION_LOG_RAW=1`` и только
        локально (в Notion не уходит). Нужен для разбора «почему ответ плохой».
    chat_id:
        Идентификатор диалога: одинаков у всех запросов одного разговора.
        Вместе с ``turn`` позволяет восстановить цепочку «вопрос → уточнение →
        ответ», не полагаясь на порядок строк.
    turn:
        Номер запроса внутри диалога (1, 2, 3…). Сбрасывается «Новым диалогом».
    answer:
        Полный текст ответа, который увидел пользователь (сводка, уточняющий
        вопрос, отказ или ошибка). В Notion не уходит.
    verdict:
        ``True``, если расчёт действительно построен (``status="ok"`` и есть
        сводка). ``False`` для уточнений/отказов/ошибок.
    latency_sec:
        Длительность обработки запроса в секундах (замер UI).
    data_source:
        Подпись источника из UI (``load_dataframe``): ``1C``, ``демо-данные``
        или имя загруженного файла. Пустая строка — источник неизвестен
        (строки, записанные до появления колонки). Задним числом не заполняется.
    """

    session_id: str
    question: str = ""
    provider: str | None = None
    model: str | None = None
    dsl: dict[str, Any] | None = None
    summary: dict[str, Any] | None = None
    raw_response: str | None = None
    chat_id: str | None = None
    turn: int | None = None
    answer: str | None = None
    status: str = "ok"
    verdict: bool = False
    latency_sec: float | None = None
    prompt_tokens: int | None = None
    output_tokens: int | None = None
    degraded: bool = False
    ts: str | None = None
    data_source: str = ""

    def __post_init__(self) -> None:
        # ``degraded`` считается по исходному статусу: после маппинга
        # «fallback» уже неотличим от «ok».
        was_degraded = is_degraded(self.status)
        object.__setattr__(self, "status", journal_status(self.status))
        object.__setattr__(self, "degraded", bool(self.degraded or was_degraded))
        # Сырой ответ пишется только при явном флаге: по умолчанию журнал
        # остаётся метаданными, а не копией промптов и ответов.
        if not raw_logging_enabled():
            object.__setattr__(self, "raw_response", None)
        else:
            object.__setattr__(
                self, "raw_response", clip_raw_response(self.raw_response)
            )

    def row(self) -> tuple[Any, ...]:
        """Значения для INSERT в порядке колонок :data:`_COLUMNS`."""
        dsl_text = (
            json.dumps(self.dsl, ensure_ascii=False, default=str)
            if self.dsl is not None
            else None
        )
        summary_text = (
            json.dumps(self.summary, ensure_ascii=False, default=str)
            if self.summary is not None
            else None
        )
        return (
            self.ts or datetime.now().astimezone().isoformat(timespec="seconds"),
            str(self.session_id),
            str(self.question or ""),
            self.provider,
            self.model,
            dsl_text,
            summary_text,
            self.raw_response,
            None if self.chat_id is None else str(self.chat_id),
            None if self.turn is None else int(self.turn),
            clip_answer(self.answer),
            self.status,
            1 if self.verdict else 0,
            float(self.latency_sec) if self.latency_sec is not None else None,
            self.prompt_tokens,
            self.output_tokens,
            1 if self.degraded else 0,
            str(self.data_source or "").strip(),
        )


_COLUMNS = (
    "ts",
    "session_id",
    "question",
    "provider",
    "model",
    "dsl",
    "summary",
    "raw_response",
    "chat_id",
    "turn",
    "answer",
    "status",
    "verdict",
    "latency_sec",
    "prompt_tokens",
    "output_tokens",
    "degraded",
    "data_source",
)


class SessionLog:
    """Потокобезопасная запись журнала с гарантией «не сломать ответ».

    Parameters
    ----------
    db_path:
        Явный путь БД; ``None`` — путь из окружения на момент записи (это
        позволяет тестам и развёртываниям переопределять файл).

    Notes
    -----
    Соединение открывается на каждую запись: SQLite в режиме WAL допускает
    параллельное чтение экспортёром, а короткая транзакция не держит файл.
    Никакие исключения наружу не выходят — это осознанный контракт.
    """

    def __init__(self, db_path: str | os.PathLike[str] | None = None) -> None:
        self._fixed_path = Path(db_path) if db_path is not None else None

    @property
    def path(self) -> Path:
        """Путь к файлу БД (для логов, тестов и экспортёра)."""
        return self._fixed_path or resolve_db_path()

    def record(self, session: SessionRecord) -> bool:
        """Записать сессию; вернуть ``True`` при успехе. Ошибки не пробрасываются."""
        if not isinstance(session, SessionRecord):  # защита от случайного dict
            return False
        target = self.path
        try:
            self._write(target, session.row())
            return True
        except (sqlite3.Error, OSError):
            # Повтор один раз: файл мог быть занят коротким экспортом (WAL).
            try:
                time.sleep(0.05)
                self._write(target, session.row())
                return True
            except Exception:
                logger.warning(
                    "Не удалось записать сессию в журнал %s", target, exc_info=True
                )
                return False
        except Exception:
            logger.warning(
                "Не удалось записать сессию в журнал %s", target, exc_info=True
            )
            return False

    @staticmethod
    def _write(target: Path, values: tuple[Any, ...]) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(target, timeout=5.0)
        try:
            _ensure_schema(connection)
            placeholders = ", ".join("?" for _ in _COLUMNS)
            connection.execute(
                f"INSERT INTO sessions ({', '.join(_COLUMNS)}) VALUES ({placeholders})",
                values,
            )
            connection.commit()
        finally:
            connection.close()


def record_request(
    *,
    session_id: str,
    question: str,
    provider: str | None,
    model: str | None,
    dsl: dict[str, Any] | None,
    status: str | None,
    verdict: bool,
    summary: dict[str, Any] | None = None,
    raw_response: str | None = None,
    chat_id: str | None = None,
    turn: int | None = None,
    answer: str | None = None,
    latency_sec: float | None = None,
    prompt_tokens: int | None = None,
    output_tokens: int | None = None,
    data_source: str | None = None,
    degraded: bool | None = None,
    db_path: str | os.PathLike[str] | None = None,
) -> bool:
    """Собрать :class:`SessionRecord` и записать его — без исключений наружу.

    Тонкая обёртка для UI: один вызов на запрос. ``degraded`` берётся из
    статуса ``fallback`` или из явного флага (лимит уточнений ADR 0007).
    Ошибки записи гасятся внутри :class:`SessionLog`. Возвращаемое
    значение нужно только тестам и логам — ответ пользователю от него не зависит.
    """
    try:
        return SessionLog(db_path).record(
            SessionRecord(
                session_id=session_id,
                question=question,
                provider=provider,
                model=model,
                dsl=dsl,
                summary=summary,
                raw_response=raw_response,
                chat_id=chat_id,
                turn=turn,
                answer=answer,
                status=str(status or "error"),
                verdict=verdict,
                latency_sec=latency_sec,
                prompt_tokens=prompt_tokens,
                output_tokens=output_tokens,
                data_source="" if data_source is None else str(data_source),
                degraded=bool(degraded),
            )
        )
    except Exception:
        logger.warning("Сбой журнала сессий: запись пропущена", exc_info=True)
        return False


def summary_to_json(payload: Any) -> dict[str, Any] | None:
    """Сводка ``SummaryPayload`` → JSON-безопасный dict (или ``None``).

    Дублировать поля не нужно: берём ровно те, что показывает UI, — текст,
    интент, значения, периоды и сущности. Функция терпима к ``None`` и к
    объектам без нужных атрибутов: журнал не должен падать из-за формата сводки.
    """
    if payload is None:
        return None
    try:
        values = [
            {
                "item": str(value.get("item", "")),
                "metric": str(value.get("metric", "")),
                "period": str(value.get("period", "")),
                "value": value.get("value"),
            }
            for value in (getattr(payload, "values", ()) or ())
            if isinstance(value, dict)
        ]
        return {
            "text": str(getattr(payload, "text", "") or ""),
            "intent": str(getattr(payload, "intent", "") or ""),
            "values": values,
            "periods": [str(item) for item in (getattr(payload, "periods", ()) or ())],
            "entities": [str(item) for item in (getattr(payload, "entities", ()) or ())],
        }
    except Exception:
        logger.warning("Не удалось сериализовать сводку результата", exc_info=True)
        return None


def result_table(dataframe: Any, *, limit: int = 20) -> dict[str, Any] | None:
    """Снимок таблицы результата (``DataFrame``) для журнала и отчёта.

    Хранится «как показано»: список колонок и строки значений, приведённые к
    JSON-безопасному виду. Ограничение ``limit`` защищает журнал от гигантских
    выборок (в отчёт едет верхняя часть таблицы, как и в UI).
    """
    if dataframe is None:
        return None
    try:
        frame = dataframe.head(max(1, int(limit)))
        columns = [str(column) for column in frame.columns]
        rows: list[list[Any]] = []
        for record in frame.itertuples(index=False, name=None):
            rows.append([_jsonable(value) for value in record])
        return {"columns": columns, "rows": rows}
    except Exception:
        logger.warning("Не удалось сохранить таблицу результата", exc_info=True)
        return None


def _jsonable(value: Any) -> Any:
    """Привести значение DataFrame к JSON-безопасному (NaN/даты/числа)."""
    if value is None:
        return None
    try:
        import math

        if isinstance(value, float) and math.isnan(value):
            return None
    except Exception:  # pragma: no cover - защита от экзотических типов
        return str(value)
    if isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        try:
            return isoformat()
        except Exception:  # pragma: no cover
            return str(value)
    item = getattr(value, "item", None)
    if callable(item):  # numpy-скаляры
        try:
            return item()
        except Exception:  # pragma: no cover
            return str(value)
    return str(value)


def _ensure_schema(connection: sqlite3.Connection) -> None:
    """Создать таблицу/индекс, добавить новые колонки и включить WAL.

    ``summary`` появился позже первой версии журнала: у существующих баз колонки
    нет, поэтому добавляем её через ``ALTER TABLE`` (данные сохраняются, повторный
    вызов ничего не делает). ``data_source`` добавляется так же, с пустым
    default: старые строки не помечаются ни как 1С, ни как демо.
    """
    connection.execute(_SCHEMA)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(sessions)")}
    if "summary" not in columns:
        connection.execute("ALTER TABLE sessions ADD COLUMN summary TEXT")
    if "raw_response" not in columns:
        connection.execute("ALTER TABLE sessions ADD COLUMN raw_response TEXT")
    if "chat_id" not in columns:
        connection.execute("ALTER TABLE sessions ADD COLUMN chat_id TEXT")
    if "turn" not in columns:
        connection.execute("ALTER TABLE sessions ADD COLUMN turn INTEGER")
    if "answer" not in columns:
        connection.execute("ALTER TABLE sessions ADD COLUMN answer TEXT")
    if "data_source" not in columns:
        connection.execute(
            "ALTER TABLE sessions ADD COLUMN data_source TEXT NOT NULL DEFAULT ''"
        )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_sessions_chat ON sessions(chat_id, turn)"
    )
    connection.execute(_INDEX)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:  # сетевой диск/только чтение — не повод падать
        pass


def _connect_readonly(target: Path) -> sqlite3.Connection:
    return sqlite3.connect(target, timeout=5.0)


def _has_column(target: Path, name: str) -> bool:
    """Есть ли колонка в таблице ``sessions`` (старые базы без миграции)."""
    if not target.exists():
        return False
    try:
        connection = _connect_readonly(target)
    except sqlite3.Error:
        return False
    try:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(sessions)").fetchall()
        }
    except sqlite3.Error:
        return False
    finally:
        connection.close()
    return name in columns


def _read_rows(
    target: Path,
    *,
    where: str,
    params: Sequence[Any],
    limit: int | None,
) -> list[dict[str, Any]]:
    """Общий SELECT журнала: одни и те же колонки для выгрузки и дозаполнения.

    ``limit=None`` — без ограничения (дозаполнение истории смотрит все строки).
    Отсутствие файла/таблицы — не ошибка: возвращается пустой список, чтобы
    экспортёр на чистой установке завершался штатно.
    """
    if not target.exists():
        return []
    try:
        connection = _connect_readonly(target)
    except sqlite3.Error:
        return []
    try:
        connection.row_factory = sqlite3.Row
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(sessions)").fetchall()
        }
        summary_column = "summary" if "summary" in columns else "NULL AS summary"
        source_column = (
            "COALESCE(data_source, '') AS data_source"
            if "data_source" in columns
            else "'' AS data_source"
        )
        sql = f"""
            SELECT id, ts, session_id, question, provider, model, dsl,
                   {summary_column}, status, verdict, latency_sec,
                   prompt_tokens, output_tokens, degraded, {source_column}
            FROM sessions
            WHERE {where}
            ORDER BY id
        """
        arguments: list[Any] = list(params)
        if limit is not None:
            sql += " LIMIT ?"
            arguments.append(max(1, int(limit)))
        rows = connection.execute(sql, tuple(arguments)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        connection.close()
    return [dict(row) for row in rows]


def pending_records(
    db_path: str | os.PathLike[str] | None = None,
    *,
    limit: int = 50,
    chat_id: str | None = None,
) -> list[dict[str, Any]]:
    """Прочитать записи, ещё не выгруженные в Notion (``exported_at IS NULL``).

    ``chat_id`` ограничивает выборку одним диалогом (кнопка «Выгрузить» в UI);
    ``None`` — все невыгруженные строки (CLI-сценарий).
    """
    target = Path(db_path) if db_path is not None else resolve_db_path()
    where = "exported_at IS NULL"
    params: list[Any] = []
    if chat_id is not None:
        if not _has_column(target, "chat_id"):
            return []
        where += " AND chat_id = ?"
        params.append(str(chat_id))
    return _read_rows(target, where=where, params=params, limit=limit)


def exported_records(
    db_path: str | os.PathLike[str] | None = None,
) -> list[dict[str, Any]]:
    """Прочитать уже выгруженные записи (``exported_at IS NOT NULL``).

    Нужно дозаполнению истории: страницы в Notion уже есть, а свойства,
    появившиеся позже (модель, id сессии), надо проставить по журналу.
    """
    target = Path(db_path) if db_path is not None else resolve_db_path()
    return _read_rows(
        target, where="exported_at IS NOT NULL", params=[], limit=None
    )


def mark_exported(
    db_path: str | os.PathLike[str] | None,
    row_id: int,
    page_id: str,
) -> None:
    """Отметить запись выгруженной: ``notion_page_id`` + ``exported_at``.

    Идемпотентно: повторный вызов перезапишет те же значения. Ошибки SQLite
    пробрасываются — экспортёр обязан знать, что отметка не легла.
    """
    target = Path(db_path) if db_path is not None else resolve_db_path()
    connection = sqlite3.connect(target, timeout=5.0)
    try:
        connection.execute(
            "UPDATE sessions SET notion_page_id = ?, exported_at = ? WHERE id = ?",
            (str(page_id), datetime.now().astimezone().isoformat(timespec="seconds"), int(row_id)),
        )
        connection.commit()
    finally:
        connection.close()
