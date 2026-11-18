# GoldSet 6.4

Нормализованный набор «вопрос → ожидаемый DSL» для агентного пути
(`use_llm=True`). Источник — `GoldenSet.py` редакции 6.3, переведённый на
канонический контракт по ADR 0013 и ТЗ 6.4.

## Состав

| Файл | Назначение |
|---|---|
| `cases.json` | 50 кейсов: вопрос, история, ожидаемый статус/DSL, факты |
| `schema.py` | загрузка, валидация, нормализация DSL |
| `checks.py` | факт-чек: все факты, ярлыки, числа по payload |
| `cassettes/` | записанные ответы модели для replay в CI |
| `../fixtures/golden_sales.csv` | синтетическая витрина с сущностями из кейсов |

## Статусы

- `ok` — 31 кейс: есть канонический DSL, исполняется `dispatch_intent`;
- `clarify` — 7: не хватает данных, ответ — уточнение;
- `refuse` — 12: честный отказ с объяснением;
- `xfail` — 1 (`test_033_vat`): `revenue_vat` вне MVP, памятка на будущее.

`expected_intent="UNKNOWN"` используется только для `clarify`/`refuse`.

## Канонический DSL

Имена и параметры — как в `analytics.INTENT_PARAMETERS` и Pydantic-моделях
`llm_agent`. Устаревшие обозначения 6.3 (`direction`, `dimension`,
`granularity`, `metrics`, `products`) запрещены и проверяются тестом.

| 6.3 | 6.4 |
|---|---|
| `direction: top/bottom` | `ascending: false/true` |
| `dimension` | `group_by` |
| `granularity` | `freq: D/W/M/Q/Y` |
| `metrics: [revenue/quantity]` | `metric` |
| `limit` у compare | `top_n` |
| `period1: "A_B"` | `period1: ["A", "B"]` |
| `group_filter/product_filter: list` | `str` (один элемент) |
| `result_entities` внутри DSL | отдельное поле кейса |

У `total` метрики нет: интент возвращает и выручку, и количество. Поэтому
кейс 032 проверяет `metric="quantity"` в фактах, но DSL остаётся `total` с
датами.

## Изменённые ожидания (ADR 0013 D3/D4)

| Кейс | Почему | Ожидание |
|---|---|---|
| 008 | `products` у compare вне MVP | `refuse` |
| 023 | `products` у compare вне MVP | `refuse` |
| 029 | несколько `product_filter` вне MVP | `refuse` |
| 009 | `period2` обязателен | `clarify(period2)` |
| 016 | `period2` обязателен; `product_filter` у counterparty вне MVP | `clarify(period2)` |
| 033 | `revenue_vat` вне MVP | `xfail` + `refuse` |
| 036 | дефолт `limit` в контракте — 10, а не 5 | `limit` не передаётся |

## Проверки

```powershell
python -m pytest -q tests/test_golden_dataset.py tests/test_golden_facts.py tests/test_golden_cassettes.py tests/test_golden_expected_dsl.py
```

- `test_golden_dataset.py` — 50 кейсов, канонический DSL, статусы, периоды,
  фильтры, покрытие фикстуры.
- `test_golden_facts.py` — факт-чек проверяет **все** факты; числа — только по
  `SummaryPayload`; заглушка «все» находит агрегат по метрике и периоду.
- `test_golden_cassettes.py` — формат кассет; синтетический пример не выдаётся
  за запись модели.
- `test_golden_expected_dsl.py` — 31 ok-кейс на expected DSL: `validate_decision
  → execute` на `golden_sales.csv` → факт-чек. Без LLM и без графа; проверяет
  pipeline, фикстуру и формат сводки.

## Кассеты

Пока есть только `cassettes/test_001_top_simple.json` с
`source=synthetic_example`. Реальные кассеты записываются после реализации
графа (этап 4); формат — в `cassettes/README.md`. Replay в CI не должен
обращаться к сети.

## Что дальше

1. Записать кассеты для 50 кейсов на эталонной модели.
2. Подключить `check_facts` к реальному `summary.py` и `SummaryPayload`.
3. Добавить live-прогон и judge по ТЗ 6.4 §6.4.
