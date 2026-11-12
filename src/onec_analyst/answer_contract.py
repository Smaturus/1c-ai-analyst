"""Контракт ответа интеграционного слоя (MCP-фасад).

Модуль фиксирует обязательные части любого ответа агента: период, дата данных,
параметры среза, пометка об усечении и текст отказа. Он не зависит от pandas и
от MCP-транспорта, поэтому контракт проверяется обычными тестами и не может
разойтись с демо-диалогом.

Правила приёмки BS-01 (условие 1):

* период и дата данных присутствуют в каждом ответе;
* если период не назван пользователем, он берётся из границ витрины, и это
  явно помечается отдельной строкой;
* период никогда не наследуется из истории диалога;
* отсутствие даты выгрузки не замалчивается, а пишется словами.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Final, Mapping, Sequence

__all__ = [
    "AnswerMeta",
    "PeriodResolution",
    "format_answer",
    "format_date",
    "format_period",
    "format_refusal",
    "resolve_period",
]

_DATE_FORMAT: Final[str] = "%d.%m.%Y"
_DATETIME_FORMAT: Final[str] = "%d.%m.%Y %H:%M"

UNSPECIFIED_PERIOD_NOTE: Final[str] = (
    "Период не указан — взят весь доступный период витрины."
)
UNKNOWN_PERIOD: Final[str] = "Период: не определён"
UNKNOWN_AS_OF: Final[str] = "Данные: дата выгрузки неизвестна"
EMPTY_DATASET: Final[str] = "Данные: витрина не загружена"


@dataclass(frozen=True)
class PeriodResolution:
    """Границы периода и признак, что период назвал пользователь."""

    start: date | None = None
    end: date | None = None
    specified: bool = False

    @property
    def label(self) -> str:
        """Человекочитаемая подпись периода: ``01.01.2024–31.12.2024``."""
        return format_period(self.start, self.end)


@dataclass(frozen=True)
class AnswerMeta:
    """Метаданные, которые обязаны попасть в любой ответ инструмента.

    Attributes
    ----------
    intent:
        Имя среза (интента), по которому построен ответ.
    params:
        Параметры среза (уже провалидированные).
    period:
        Разрешённый период с признаком ``specified``.
    as_of:
        Момент выгрузки данных, если он известен.
    file_mtime:
        Время файла витрины — запасной источник даты данных.
    shown, total:
        Сколько строк показано и сколько всего (для пометки об усечении).
    notes:
        Дополнительные пометки, которые обязаны попасть в шапку ответа.
    """

    intent: str
    params: Mapping[str, Any] = field(default_factory=dict)
    period: PeriodResolution = field(default_factory=PeriodResolution)
    as_of: datetime | None = None
    file_mtime: datetime | None = None
    shown: int | None = None
    total: int | None = None
    notes: Sequence[str] = ()


def format_date(value: date | datetime) -> str:
    """Дата в формате проекта: ``01.01.2024``."""
    return value.strftime(_DATE_FORMAT)


def format_period(start: date | None, end: date | None) -> str:
    """Подпись периода; незаданная граница показывается многоточием."""
    if start is None and end is None:
        return "не определён"
    if start is None:
        return f"…–{format_date(end)}"  # type: ignore[arg-type]
    if end is None:
        return f"{format_date(start)}–…"
    return f"{format_date(start)}–{format_date(end)}"


def resolve_period(
    start: date | datetime | None,
    end: date | datetime | None,
    *,
    data_min: date | datetime | None,
    data_max: date | datetime | None,
) -> PeriodResolution:
    """Разрешить период запроса, не наследуя его из истории диалога.

    Названные пользователем границы сохраняются; недостающие подставляются из
    границ витрины. Признак ``specified`` показывает, называл ли пользователь
    хотя бы одну границу.
    """
    given_start = _as_date(start)
    given_end = _as_date(end)
    resolved_start = given_start if given_start is not None else _as_date(data_min)
    resolved_end = given_end if given_end is not None else _as_date(data_max)
    if resolved_start is not None and resolved_end is not None:
        if resolved_start > resolved_end:
            raise ValueError(
                "начало периода "
                f"{format_date(resolved_start)} позже конца {format_date(resolved_end)}"
            )
    return PeriodResolution(
        start=resolved_start,
        end=resolved_end,
        specified=given_start is not None or given_end is not None,
    )


def format_answer(body: str, meta: AnswerMeta) -> str:
    """Собрать ответ с обязательной шапкой и пометкой об усечении."""
    lines = _header_lines(meta)
    text = (body or "").strip()
    if text:
        lines.extend(["", text])
    truncation = _truncation_line(meta)
    if truncation:
        lines.extend(["", truncation])
    return "\n".join(lines)


def format_refusal(
    reason: str,
    available: Sequence[str],
    *,
    meta: AnswerMeta | None = None,
) -> str:
    """Отказ вместо похожего ответа: причина, данные и список доступных срезов."""
    lines = [f"Не поддерживается: {reason.rstrip('.')}."]
    if meta is not None:
        lines.extend(_header_lines(meta, include_intent=False))
    else:
        lines.extend([UNKNOWN_PERIOD, EMPTY_DATASET])
    lines.append(f"Доступно: {', '.join(available)}.")
    return "\n".join(lines)


def _header_lines(meta: AnswerMeta, *, include_intent: bool = True) -> list[str]:
    lines: list[str] = []
    if meta.period.start is None and meta.period.end is None:
        lines.append(UNKNOWN_PERIOD)
    else:
        lines.append(f"Период: {meta.period.label}")
        if not meta.period.specified:
            lines.append(UNSPECIFIED_PERIOD_NOTE)
    lines.append(_data_line(meta))
    if include_intent:
        lines.append(_intent_line(meta))
    lines.extend(note for note in meta.notes if note)
    return lines


def _data_line(meta: AnswerMeta) -> str:
    if meta.as_of is not None:
        return f"Данные: выгрузка {meta.as_of.strftime(_DATETIME_FORMAT)}"
    if meta.file_mtime is not None:
        return f"Данные: время файла {meta.file_mtime.strftime(_DATETIME_FORMAT)}"
    return UNKNOWN_AS_OF


def _intent_line(meta: AnswerMeta) -> str:
    parts: list[str] = []
    for key in sorted(meta.params):
        rendered = _format_param(meta.params[key])
        if rendered is not None:
            parts.append(f"{key}={rendered}")
    if not parts:
        return f"Срез: {meta.intent}"
    return f"Срез: {meta.intent} ({', '.join(parts)})"


def _format_param(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (list, tuple)) and len(value) == 2:
        first, second = (_format_param(item) for item in value)
        if first is not None and second is not None:
            return f"{first}…{second}"
    return str(value)


def _truncation_line(meta: AnswerMeta) -> str:
    if meta.total is None:
        if meta.shown is None:
            return ""
        return f"Показаны все {meta.shown}."
    if meta.shown is None:
        return f"Всего строк: {meta.total}."
    if meta.shown >= meta.total:
        return f"Показаны все {meta.total}."
    return f"Показаны первые {meta.shown} из {meta.total}."


def _as_date(value: date | datetime | None) -> date | None:
    """Привести дату к ``date``; ``None``/``NaT`` остаются ``None``."""
    if value is None:
        return None
    if value != value:  # pandas NaT/NaN не равен сам себе
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raise TypeError(f"Ожидалась дата, получено {type(value)!r}")
