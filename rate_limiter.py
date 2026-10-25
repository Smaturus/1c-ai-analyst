"""Учёт вызовов LLM для публичного демо-режима.

Ограничивает количество обращений к платному API, чтобы демо-стенд не тратил
токены и не перегружался:

* **на сессию** — по умолчанию 15 успешных вызовов LLM в день;
* **глобально** — по умолчанию 100 вызовов в день на всё приложение.

Учёт хранится в SQLite (файл ``rate_limit.db`` в корне проекта, в git не
попадает). Ключ — ``(date, session_id)``; смена календарной даты автоматически
обнуляет счётчики (в таблице просто нет строк за новую дату).

Ошибки SQLite (например, файл заблокирован) не роняют приложение: они
оборачиваются в :class:`RateLimiterError`, а UI переключается на офлайн-режим
правил.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

#: Лимит успешных вызовов LLM на одну сессию в день.
MAX_PER_SESSION = 15
#: Глобальный дневной лимит вызовов LLM на всё приложение.
MAX_GLOBAL = 100
#: Путь к базе по умолчанию; можно переопределить переменной окружения.
DEFAULT_DB_PATH = Path(__file__).resolve().parent / "rate_limit.db"
DB_ENV_VAR = "RATE_LIMIT_DB"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_calls (
    date TEXT NOT NULL,
    session_id TEXT NOT NULL,
    request_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (date, session_id)
)
"""


class RateLimiterError(RuntimeError):
    """Учёт лимитов недоступен (SQLite вернул ошибку/файл заблокирован)."""


@dataclass(frozen=True)
class LimitStatus:
    """Текущий статус лимитов для сессии.

    Attributes
    ----------
    allowed:
        Можно ли ещё вызывать LLM.
    used_session:
        Сколько вызовов уже сделано этой сессией сегодня.
    used_global:
        Сколько вызовов сделано всеми сессиями сегодня.
    limit_session, limit_global:
        Действующие лимиты.
    reason:
        Причина отказа: ``"session"``, ``"global"`` или ``None`` (можно).
    """

    allowed: bool
    used_session: int
    used_global: int
    limit_session: int
    limit_global: int
    reason: str | None = None


class RateLimiter:
    """Простой учёт вызовов LLM в SQLite (без внешних зависимостей).

    Parameters
    ----------
    db_path:
        Путь к файлу базы. По умолчанию — ``rate_limit.db`` в корне проекта
        (или значение ``RATE_LIMIT_DB``).
    today:
        Фиксированная дата для воспроизводимых тестов; по умолчанию
        ``date.today()``.
    """

    def __init__(self, db_path: str | os.PathLike[str] | None = None, *, today: date | None = None) -> None:
        resolved = db_path or os.environ.get(DB_ENV_VAR) or DEFAULT_DB_PATH
        self._db_path = Path(resolved)
        self._today = today

    @property
    def db_path(self) -> Path:
        """Путь к файлу базы (для логов и тестов)."""
        return self._db_path

    def _current_date(self) -> str:
        """Календарная дата учёта в формате ``YYYY-MM-DD``."""
        return (self._today or date.today()).isoformat()

    def _connect(self) -> sqlite3.Connection:
        """Открыть базу и создать таблицу (с таймаутом на блокировки)."""
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self._db_path, timeout=2.0)
            connection.execute(_SCHEMA)
            return connection
        except (sqlite3.Error, OSError) as exc:
            raise RateLimiterError(
                f"Не удалось открыть базу учёта лимитов {self._db_path}: {exc}"
            ) from exc

    def check_limit(
        self,
        session_id: str,
        max_per_session: int = MAX_PER_SESSION,
        max_global: int = MAX_GLOBAL,
    ) -> LimitStatus:
        """Проверить, можно ли сессии ещё вызывать LLM.

        Raises
        ------
        RateLimiterError
            Если база недоступна/заблокирована.
        """
        today = self._current_date()
        connection = self._connect()
        try:
            with connection:
                session_row = connection.execute(
                    "SELECT request_count FROM llm_calls WHERE date = ? AND session_id = ?",
                    (today, session_id),
                ).fetchone()
                total_row = connection.execute(
                    "SELECT COALESCE(SUM(request_count), 0) FROM llm_calls WHERE date = ?",
                    (today,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise RateLimiterError(f"Ошибка чтения базы лимитов: {exc}") from exc
        finally:
            connection.close()

        used_session = int(session_row[0]) if session_row else 0
        used_global = int(total_row[0]) if total_row else 0
        reason: str | None = None
        if used_session >= max_per_session:
            reason = "session"
        elif used_global >= max_global:
            reason = "global"
        return LimitStatus(
            allowed=reason is None,
            used_session=used_session,
            used_global=used_global,
            limit_session=max_per_session,
            limit_global=max_global,
            reason=reason,
        )

    def increment(self, session_id: str) -> int:
        """Учесть один успешный вызов LLM; вернуть счётчик сессии за сегодня.

        Raises
        ------
        RateLimiterError
            Если база недоступна/заблокирована.
        """
        today = self._current_date()
        connection = self._connect()
        try:
            with connection:
                connection.execute(
                    """
                    INSERT INTO llm_calls (date, session_id, request_count)
                    VALUES (?, ?, 1)
                    ON CONFLICT(date, session_id)
                    DO UPDATE SET request_count = request_count + 1
                    """,
                    (today, session_id),
                )
                row = connection.execute(
                    "SELECT request_count FROM llm_calls WHERE date = ? AND session_id = ?",
                    (today, session_id),
                ).fetchone()
        except sqlite3.Error as exc:
            raise RateLimiterError(f"Ошибка записи в базу лимитов: {exc}") from exc
        finally:
            connection.close()
        return int(row[0]) if row else 0

    def reset_session(self, session_id: str) -> None:
        """Сбросить сегодняшний счётчик сессии.

        В UI вызывается только при ``RATE_LIMIT_ALLOW_RESET``; иначе сброс —
        только сменой даты или прямой вызов API модуля.

        Raises
        ------
        RateLimiterError
            Если база недоступна/заблокирована.
        """
        today = self._current_date()
        connection = self._connect()
        try:
            with connection:
                connection.execute(
                    "DELETE FROM llm_calls WHERE date = ? AND session_id = ?",
                    (today, session_id),
                )
        except sqlite3.Error as exc:
            raise RateLimiterError(f"Ошибка сброса лимита: {exc}") from exc
        finally:
            connection.close()


__all__ = [
    "MAX_GLOBAL",
    "MAX_PER_SESSION",
    "DEFAULT_DB_PATH",
    "DB_ENV_VAR",
    "LimitStatus",
    "RateLimiter",
    "RateLimiterError",
]
