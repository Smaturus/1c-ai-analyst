"""Аудит шагов LangGraph-агента без сырого вопроса и секретов.

Пишет события в тот же ``AuditLog``, что и MCP-сервер (``.mcp_state/audit.jsonl``),
но с префиксом шагов графа. Ошибка записи аудита не должна ломать ответ:
``AuditLog`` уже ловит ``OSError``, а helper дополнительно не пробрасывает
исключения наружу.

В журнал попадают только перечисленные поля: статус, интент, счётчики,
канонические ``missing``, признак fallback, провайдер/модель. Текст вопроса,
ответа модели и секреты не пишутся.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: Поля, которые разрешено писать в аудит. Всё остальное отбрасывается:
#: так ``question``/``resolved_question``/``raw_response`` не попадут в журнал
#: даже при будущем расширении вызовов.
ALLOWED_FIELDS = frozenset(
    {
        "status",
        "intent",
        "retry_count",
        "llm_calls",
        "missing",
        "fallback",
        "provider",
        "model",
        "error_type",
        "step",
        "duration_ms",
    }
)


def audit_event(state: Mapping[str, Any], event: str, **fields: Any) -> None:
    """Записать шаг графа в ``state['audit']``, если журнал передан.

    Поля фильтруются по :data:`ALLOWED_FIELDS`; ``None`` не пишется.
    Дополнительно пишется ``step=event`` — то же значение, что и ``event``,
    но в поле ``step`` (по ТЗ 6.4 §4.4); ``event`` не переименовывается, потому
    что его использует MCP-аудит.
    Исключение записи не пробрасывается: аудит — побочный канал, а не
    условие ответа.
    """
    log = state.get("audit")
    if log is None:
        return
    safe = {
        key: value
        for key, value in {**fields, "step": str(event)}.items()
        if key in ALLOWED_FIELDS and value is not None
    }
    try:
        log.record(str(event), **safe)
    except Exception:
        return


__all__ = ["ALLOWED_FIELDS", "audit_event"]
