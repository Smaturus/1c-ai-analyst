"""Детерминированный факт-чек GoldSet: все факты + числа по payload.

Редакция 6.3 проверяла только первый факт: ветки заканчивались ``return``.
Здесь цикл не прерывается, поэтому кейсы с 2–3 фактами проверяются целиком.

Числа не парсятся из отформатированной сводки. Для них передаётся
``SummaryPayload`` (словарь) с элементами ``values``:
``{"values": [{"item": ..., "metric": ..., "period": ..., "value": ...}]}``.
Так проверяется верность ответа расчёту, а не работа форматера.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from tests.golden.schema import GENERIC_ITEMS, MISSING_ALIASES

METRIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "revenue": ("выручка", "продаж", "руб"),
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
)

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


def _check_value(summary: str, fact: Mapping[str, Any], payload: Mapping[str, Any] | None) -> None:
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
    variants = PERIOD_VARIANTS.get(period, (period,))
    assert any(variant.lower() in summary for variant in variants), (
        f"Период {period!r} не найден ни в одном формате: {summary!r}"
    )

    if "value" not in fact:
        return
    assert payload is not None, (
        "value-факт с числом требует SummaryPayload: числа не парсятся из текста"
    )
    expected = float(fact["value"])
    actual = _payload_value(payload, item=item, metric=metric, period=period)
    tolerance = max(0.01, abs(expected) * 1e-6)
    assert abs(actual - expected) <= tolerance, (
        f"Число в payload {actual!r} не совпало с ожидаемым {expected!r} "
        f"для {item!r}/{metric}/{period}"
    )


def _payload_value(
    payload: Mapping[str, Any], *, item: str, metric: str, period: str
) -> float:
    for row in payload.get("values", []):
        if (
            str(row.get("item", "")) == item
            and str(row.get("metric", "")) == metric
            and str(row.get("period", "")) == period
        ):
            return float(row["value"])
    raise AssertionError(
        f"В payload нет значения для {item!r}/{metric}/{period}: {payload!r}"
    )


def check_facts(
    summary: str,
    facts: Sequence[Mapping[str, Any]],
    *,
    clarification_missing: Sequence[str] = (),
    payload: Mapping[str, Any] | None = None,
) -> None:
    """Проверить ВСЕ факты кейса; при ошибке — с номером факта.

    ``clarification_missing`` — то, что вернул ClarificationPlan.missing.
    Если он передан, clarify-факт проверяется по нему, а не по тексту.
    """
    text = summary.lower()
    for index, fact in enumerate(facts, start=1):
        fact_type = fact.get("type")
        if fact_type == "refusal":
            markers = fact.get("reason_contains")
            if markers:
                missing_markers = [
                    str(marker).lower() for marker in markers if str(marker).lower() not in text
                ]
                assert not missing_markers, (
                    f"Факт {index}: в отказе нет объяснения {missing_markers}: {summary!r}"
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
                _check_value(text, fact, payload)
            except AssertionError as exc:
                raise AssertionError(f"Факт {index}: {exc}") from exc
            continue

        raise AssertionError(f"Факт {index}: неизвестный тип {fact_type!r}")
