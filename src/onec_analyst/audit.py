"""Журнал вызовов MCP-сервера: аудит и доля отказов.

Журнал пишется в JSONL и лежит вне репозитория по смыслу: путь по умолчанию —
``.mcp_state/audit.jsonl`` в корне проекта (каталог в ``.gitignore``), может быть
переопределён переменной ``ONEC_MCP_AUDIT_FILE``.

Зачем нужен:

* аудит: кто, когда и какой срез запросил (для 1С-данных это требование ИБ);
* метрика приёмки «доля отказов»: если больше 30 % вопросов не поддерживаются,
  решение по фазе 2 принимается досрочно;
* журнал отказов как источник требований на новые срезы.

Ошибка записи журнала не должна ломать ответ инструмента: она только пишется
в лог приложения.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from onec_analyst.paths import find_repo_root

__all__ = [
    "AUDIT_FILE_ENV",
    "DEFAULT_AUDIT_FILE",
    "AuditLog",
    "default_audit_path",
    "read_audit",
    "resolve_audit_path",
    "summarize_audit",
    "to_jsonable",
]

logger = logging.getLogger("onec_analyst.audit")

AUDIT_FILE_ENV: str = "ONEC_MCP_AUDIT_FILE"
DEFAULT_AUDIT_FILE: Path = Path(".mcp_state") / "audit.jsonl"


def default_audit_path() -> Path:
    """Путь журнала по умолчанию: ``.mcp_state/audit.jsonl`` в корне проекта."""
    return find_repo_root() / DEFAULT_AUDIT_FILE


def resolve_audit_path() -> Path:
    """Путь журнала с учётом ``ONEC_MCP_AUDIT_FILE``."""
    raw = os.environ.get(AUDIT_FILE_ENV, "").strip()
    return Path(raw) if raw else default_audit_path()


def to_jsonable(value: Any) -> Any:
    """Привести значение к JSON-безопасному виду (даты — ISO-строками)."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


class AuditLog:
    """Потокобезопасная запись событий в JSONL.

    Parameters
    ----------
    path:
        Явный путь журнала; ``None`` — путь из окружения на момент записи
        (это позволяет тестам и развёртываниям переопределять файл).
    """

    def __init__(self, path: Path | None = None) -> None:
        self._fixed_path = path
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._fixed_path or resolve_audit_path()

    def record(self, event: str, **fields: Any) -> None:
        """Дописать событие; ``None``-поля пропускаются."""
        payload: dict[str, Any] = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "event": str(event),
        }
        payload.update(
            {key: to_jsonable(value) for key, value in fields.items() if value is not None}
        )
        line = json.dumps(payload, ensure_ascii=False)
        target = self.path
        with self._lock:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                logger.warning("Не удалось записать аудит в %s", target, exc_info=True)


def read_audit(path: Path | None = None) -> list[dict[str, Any]]:
    """Прочитать журнал; битые строки и отсутствие файла не считаются ошибкой."""
    target = path or resolve_audit_path()
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records


def summarize_audit(path: Path | None = None, *, days: int = 30) -> dict[str, Any]:
    """Сводка за окно: число срезов, отказов, доля отказов и топ причин."""
    records = read_audit(path)
    cutoff = datetime.now().astimezone() - timedelta(days=days)
    recent = [record for record in records if _is_recent(record, cutoff)]
    slices = sum(1 for record in recent if record.get("event") == "slice")
    refusals = [record for record in recent if record.get("event") == "refusal"]
    total = slices + len(refusals)
    reasons = Counter(str(record.get("reason", "—")) for record in refusals)
    return {
        "window_days": days,
        "slices": slices,
        "refusals": len(refusals),
        "refusal_rate": round(len(refusals) / total, 3) if total else 0.0,
        "top_refusal_reasons": reasons.most_common(5),
    }


def _is_recent(record: dict[str, Any], cutoff: datetime) -> bool:
    raw = record.get("ts")
    if not isinstance(raw, str):
        return False
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment >= cutoff
