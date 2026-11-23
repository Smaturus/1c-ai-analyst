"""Детерминированный факт-чек GoldSet: все факты + числа по payload.

Редакция 6.3 проверяла только первый факт: ветки заканчивались ``return``.
Здесь цикл не прерывается, поэтому кейсы с 2–3 фактами проверяются целиком.

Числа не парсятся из отформатированной сводки. Для них передаётся
``SummaryPayload`` (словарь) с элементами ``values``:
``{"values": [{"item": ..., "metric": ..., "period": ..., "value": ...}]}``.
Так проверяется верность ответа расчёту, а не работа форматера.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from tests.golden.schema import GENERIC_ITEMS, MISSING_ALIASES

METRIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "revenue": ("выручк", "продаж", "руб"),
    "quantity": ("шт",),
}

REFUSAL_MARKERS: tuple[str, ...] = (
    "не могу",
    "извините",
    "не относится",
    "не поддерживается",
    "не удалось",
    "жаль",
    "не является аналитическим",
    "не поддерж",
    # Формулировки отказа, которые реально дают модели: «это не аналитика
    # продаж» — объяснённый отказ, а не свободный текст.
    "не аналитика",
    "вне аналитики",
    "благодарность",
    "не вопрос",
    # Отказ на будущий/внедиапазонный период (DECISIONS #32): модель объясняет
    # причину словами, а не общими маркерами отказа.
    "будущий период",
    "в будущем",
    "данных ещё нет",
    "ещё не доступн",
    "ещё нет",
    "вне диапазона",
    "позже сегодняшней",
    "инъекц",
)

#: Маркеры падения для правила знака (007): дельта < 0.
FALL_MARKERS: tuple[str, ...] = (
    "падени",
    "снижен",
    "снизил",
    "упал",
    "уменьш",
    "просел",
    "сократ",
    "отрицательн",
    "минус",
)

#: Маркеры роста для правила знака (007): дельта > 0.
GROWTH_MARKERS: tuple[str, ...] = (
    "рост",
    "вырос",
    "увелич",
    "поднял",
    "прибав",
    "плюс",
)

#: Русские глагольные окончания, которые снимаются при стемминге причины отказа:
#: «не поддерживается» и «не поддерживаются» должны считаться одной причиной.
_VERB_SUFFIXES: tuple[str, ...] = (
    "аются",
    "яются",
    "ается",
    "яются",
    "ются",
    "ется",
    "ится",
    "тся",
    "ться",
)


def _stem(value: str) -> str:
    """Обрубить глагольное окончание («поддерживается» → «поддержива»)."""
    text = str(value or "").lower()
    for suffix in _VERB_SUFFIXES:
        if text.endswith(suffix):
            return text[: -len(suffix)]
    return text


def _contains_reason(text: str, marker: str) -> bool:
    """Причина отказа найдена в тексте: точная подстрока или её стем-форма.

    Причина из ``reason_contains`` эталона должна объяснять отказ, но модели
    варьируют окончания («не поддерживается» / «не поддерживаются»). Стемминг
    снимает только глагольное окончание, не превращая проверку в «любой отказ».
    """
    normalized = str(marker or "").lower()
    if not normalized:
        return False
    if normalized in text:
        return True
    return _stem(normalized) in text

MISSING_KEYWORDS: dict[str, str] = {
    "period": "период",
    "period1": "период",
    "period2": "период",
    "start_date": "период",
    "end_date": "период",
    "products": "товар",
    "product_filter": "товар",
    "group": "групп",
    "group_filter": "групп",
    "counterparty": "контрагент",
    "intent": "уточн",
}

PERIOD_VARIANTS: dict[str, tuple[str, ...]] = {
    "2024-Q1": ("2024-Q1", "I квартал 2024", "1 квартал 2024", "Q1 2024", "01.01.2024", "31.03.2024"),
    "2024-Q2": ("2024-Q2", "II квартал 2024", "2 квартал 2024", "Q2 2024", "01.04.2024", "30.06.2024"),
    "2024-Q3": ("2024-Q3", "III квартал 2024", "3 квартал 2024", "Q3 2024", "01.07.2024", "30.09.2024"),
    "2024-Q4": ("2024-Q4", "IV квартал 2024", "4 квартал 2024", "Q4 2024", "01.10.2024", "31.12.2024"),
    "2023-10": ("2023-10", "октябрь 2023", "10.2023", "01.10.2023", "31.10.2023"),
    "2023-11": ("2023-11", "ноябрь 2023", "11.2023", "01.11.2023", "30.11.2023"),
    "2023-12": ("2023-12", "декабрь 2023", "12.2023", "01.12.2023", "31.12.2023"),
    "2024-01": ("2024-01", "январь 2024", "01.2024", "01.01.2024", "31.01.2024"),
    "2024-03": ("2024-03", "март 2024", "03.2024", "01.03.2024", "31.03.2024"),
    "2024-05": ("2024-05", "май 2024", "05.2024", "01.05.2024", "31.05.2024"),
    "2024-10": ("2024-10", "октябрь 2024", "10.2024", "01.10.2024", "31.12.2024"),
    "2024-11": ("2024-11", "ноябрь 2024", "11.2024", "01.11.2024", "30.11.2024"),
    "1990": ("1990",),
    "2022": ("2022",),
    "2023": ("2023",),
    "2024": ("2024",),
}


_MONTH_STEMS: tuple[tuple[str, int], ...] = (
    ("январ", 1),
    ("феврал", 2),
    ("март", 3),
    ("апрел", 4),
    ("май", 5),
    ("мая", 5),
    ("июн", 6),
    ("июл", 7),
    ("август", 8),
    ("сентябр", 9),
    ("октябр", 10),
    ("ноябр", 11),
    ("декабр", 12),
)

_ISO_RANGE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s*[–—-]\s*(\d{4}-\d{2}-\d{2})$")
_DOT_RANGE_RE = re.compile(
    r"^(\d{2})\.(\d{2})\.(\d{4})\s*(?:[–—-]|по)\s*(\d{2})\.(\d{2})\.(\d{4})$"
)
_DOT_MONTH_RE = re.compile(r"^(\d{2})\.(\d{4})$")
_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_QUARTER_RE = re.compile(r"^(\d{4})-q([1-4])$")
_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
_YEAR_RE = re.compile(r"^(\d{4})$")


def _iso_to_dot(value: str) -> str:
    """``2023-11-01`` → ``01.11.2023``."""
    year, month, day = value.split("-")
    return f"{day}.{month}.{year}"


def normalize_period(value: str) -> str:
    """Привести любую подпись периода к каноническому ключу (DECISIONS #32).

    Поддержаны формы, которые реально отдают ``summary.py`` и эталон:
    ``2023``, ``2024-Q1``, ``2023-11``, ``ноябрь 2023``, ``11.2023``,
    ``2023-11-01``, диапазон ``2023-11-01–2023-12-31`` и его человекочитаемая
    форма ``период с 01.11.2023 по 31.12.2023``.
    """
    text = str(value or "").strip().lower().replace("ё", "е")
    if not text:
        return ""
    if text.startswith("период с "):
        text = text[len("период с ") :]
    text = text.replace("—", "–")
    match = _ISO_RANGE_RE.match(text)
    if match:
        return f"{match.group(1)}–{match.group(2)}"
    match = _DOT_RANGE_RE.match(text)
    if match:
        first = f"{match.group(3)}-{match.group(2)}-{match.group(1)}"
        second = f"{match.group(6)}-{match.group(5)}-{match.group(4)}"
        return f"{first}–{second}"
    if _YEAR_RE.match(text):
        return text
    match = _QUARTER_RE.match(text)
    if match:
        return f"{match.group(1)}-Q{match.group(2)}"
    if _ISO_DATE_RE.match(text):
        return text
    match = _MONTH_RE.match(text)
    if match:
        return text
    match = _DOT_MONTH_RE.match(text)
    if match:
        return f"{match.group(2)}-{match.group(1)}"
    match = re.match(r"^([а-я]+)\s+(\d{4})$", text)
    if match:
        for stem, number in _MONTH_STEMS:
            if match.group(1).startswith(stem):
                return f"{match.group(2)}-{number:02d}"
    return text


def period_variants(period: str) -> tuple[str, ...]:
    """Все варианты написания периода для текстовой сверки.

    Словарь ``PERIOD_VARIANTS`` остаётся ядром, но к нему добавляются записи,
    выведенные из канонического ключа: для диапазона — обе даты, их точечная
    форма и фраза «с … по …» (иначе текст сводки с диапазоном не сверить).
    """
    raw = str(period or "").strip()
    key = normalize_period(raw)
    variants: list[str] = []
    variants.extend(PERIOD_VARIANTS.get(raw, ()))
    if key and key != raw:
        variants.extend(PERIOD_VARIANTS.get(key, ()))
    variants.append(raw)
    variants.append(key)
    if "–" in key:
        start, end = key.split("–", 1)
        dot_start, dot_end = _iso_to_dot(start), _iso_to_dot(end)
        variants.extend(
            (
                dot_start,
                dot_end,
                f"{dot_start}–{dot_end}",
                f"{dot_start} — {dot_end}",
                f"с {dot_start} по {dot_end}",
            )
        )
    return tuple(dict.fromkeys(item for item in variants if item))


def check_delta_signs(summary: str, payload: Mapping[str, Any] | None) -> None:
    """Правило знака 007: знак дельты обязан подтверждаться текстом.

    Область — только ``metric="delta"`` (DECISIONS #31): записи ``negative_revenue``
    с отрицательной выручкой — это явление возвратов, а не падение.
    ``value < 0`` → рядом с сущностью маркер падения; ``value > 0`` → роста;
    ``value == 0`` не проверяется (трактовка «без изменений» — на реализацию).
    """
    text = (summary or "").lower()
    for row in (payload or {}).get("values", []):
        if str(row.get("metric", "")) != "delta":
            continue
        item = str(row.get("item", "")).lower()
        if not item:
            continue
        try:
            value = float(row.get("value"))
        except (TypeError, ValueError):
            continue
        index = text.find(item)
        if index < 0:
            raise AssertionError(
                f"Сущность {row.get('item')!r} из delta-записи не названа в сводке: {summary!r}"
            )
        window = text[max(0, index - 160) : index + len(item) + 160]
        if value < 0:
            assert any(marker in window for marker in FALL_MARKERS), (
                f"Для отрицательной дельты {value!r} у {row.get('item')!r} "
                f"нет маркера падения: {summary!r}"
            )
        elif value > 0:
            assert any(marker in window for marker in GROWTH_MARKERS), (
                f"Для положительной дельты {value!r} у {row.get('item')!r} "
                f"нет маркера роста: {summary!r}"
            )


def _check_value(
    summary: str,
    fact: Mapping[str, Any],
    payload: Mapping[str, Any] | None,
    *,
    strict_period: bool = True,
) -> None:
    item = str(fact.get("item", ""))
    if item and item.lower() not in GENERIC_ITEMS:
        assert item.lower() in summary, (
            f"Сущность {item!r} отсутствует в сводке: {summary!r}"
        )

    metric = str(fact.get("metric", ""))
    keywords = METRIC_KEYWORDS.get(metric)
    assert keywords is not None, f"Неизвестная метрика: {metric!r}"
    assert any(keyword in summary for keyword in keywords), (
        f"Метрика {metric!r} не упомянута в сводке: {summary!r}"
    )

    period = str(fact.get("period", ""))
    if strict_period:
        variants = period_variants(period)
        assert any(variant.lower() in summary for variant in variants), (
            f"Период {period!r} не найден ни в одном формате: {summary!r}"
        )

    if "value" not in fact:
        return
    assert payload is not None, (
        "value-факт с числом требует SummaryPayload: числа не парсятся из текста"
    )
    expected = float(fact["value"])
    actual = _payload_value(
        payload,
        item=item,
        metric=metric,
        period=period if strict_period else "",
    )
    tolerance = max(0.01, abs(expected) * 1e-6)
    assert abs(actual - expected) <= tolerance, (
        f"Число в payload {actual!r} не совпало с ожидаемым {expected!r} "
        f"для {item!r}/{metric}/{period}"
    )


def _payload_value(
    payload: Mapping[str, Any], *, item: str, metric: str, period: str
) -> float:
    """Значение из payload: точная метрика + период (или только метрика).

    Пустой ``period`` — режим цепочек (DECISIONS #32): контекст задаёт живой
    диалог, поэтому период не сверяется, но значение обязано быть однозначным.
    """
    wanted = normalize_period(period) if period else ""
    rows = [
        row
        for row in payload.get("values", [])
        if str(row.get("metric", "")) == metric
        and (not wanted or normalize_period(str(row.get("period", ""))) == wanted)
    ]
    for row in rows:
        if str(row.get("item", "")) == item:
            return float(row["value"])

    # Заглушки «товар»/«все» означают агрегат: ищем по метрике и периоду.
    if item and item.lower() not in GENERIC_ITEMS:
        raise AssertionError(
            f"В payload нет значения для {item!r}/{metric}/{period}: {payload!r}"
        )
    if len(rows) == 1:
        return float(rows[0]["value"])
    for preferred in ("Итого", "Сумма", "Возвраты", "все"):
        for row in rows:
            if str(row.get("item", "")) == preferred:
                return float(row["value"])
    if rows:
        raise AssertionError(
            f"Неоднозначный payload для {item!r}/{metric}/{period}: "
            f"{[row.get('item') for row in rows]!r}"
        )
    raise AssertionError(
        f"В payload нет значения для {item!r}/{metric}/{period}: {payload!r}"
    )


def check_facts(
    summary: str,
    facts: Sequence[Mapping[str, Any]],
    *,
    clarification_missing: Sequence[str] = (),
    payload: Mapping[str, Any] | None = None,
    strict_period: bool = True,
) -> None:
    """Проверить ВСЕ факты кейса; при ошибке — с номером факта.

    ``clarification_missing`` — то, что вернул ClarificationPlan.missing.
    Если он передан, clarify-факт проверяется по нему, а не по тексту.

    ``strict_period=False`` — режим follow-up: период задаёт живой контекст
    диалога, а не эталонный кейс (DECISIONS #32), поэтому сверяются сущность,
    метрика и значение, а период — нет.
    """
    text = summary.lower()
    for index, fact in enumerate(facts, start=1):
        fact_type = fact.get("type")
        if fact_type == "refusal":
            markers = fact.get("reason_contains")
            if markers:
                # Достаточно ХОТЯ БЫ ОДНОЙ причины (стем-форма): модели варьируют
                # формулировки, и требовать все дословно — занижать честный отказ.
                if not any(_contains_reason(text, marker) for marker in markers):
                    raise AssertionError(
                        f"Факт {index}: в отказе нет ни одной из причин "
                        f"{list(markers)}: {summary!r}"
                    )
            else:
                assert any(marker in text for marker in REFUSAL_MARKERS), (
                    f"Факт {index}: ожидался отказ с объяснением, маркеров нет: {summary!r}"
                )
            continue

        if fact_type == "clarify":
            expected = str(fact.get("missing", ""))
            aliases = MISSING_ALIASES.get(expected, frozenset({expected}))
            if clarification_missing:
                actual = {str(key) for key in clarification_missing}
                assert actual & aliases, (
                    f"Факт {index}: ожидался missing из {sorted(aliases)}, "
                    f"получено {sorted(actual)}"
                )
            else:
                keyword = MISSING_KEYWORDS.get(expected, "уточн")
                assert keyword in text or "?" in text, (
                    f"Факт {index}: уточнение не называет {keyword!r}: {summary!r}"
                )
            continue

        if fact_type == "value":
            try:
                _check_value(
                    text, fact, payload, strict_period=strict_period
                )
            except AssertionError as exc:
                raise AssertionError(f"Факт {index}: {exc}") from exc
            continue

        raise AssertionError(f"Факт {index}: неизвестный тип {fact_type!r}")
