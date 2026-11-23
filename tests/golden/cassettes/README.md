# Кассеты GoldSet

Кассета — записанный **сырой ответ модели** на одном кейсе, приведённый к уровню
`AgentDecision` и не зависящий от провайдера. CI проигрывает кассеты без сети:
парсер → Pydantic → `fix_dsl` → маршрутизация → факт-чек. Кассета хранит, что
реально вернула модель (tool call или JSON-контент) и снимок того, что граф с
этим ответом сделал (`recorded_outcome`) — а не ожидаемый DSL.

## Формат файла

Имя файла: `<case_id>.json` (например, `test_001_top_simple.json`).

```json
{
  "case_id": "test_001_top_simple",
  "source": "recorded",
  "recorded_at": "2026-11-18",
  "provider": "deepseek",
  "model": "deepseek-chat",
  "responses": [
    {
      "tool_call": {
        "name": "top_bottom_products",
        "arguments": {
          "metric": "revenue",
          "limit": 5,
          "ascending": false,
          "start_date": "2023-01-01",
          "end_date": "2023-12-31"
        }
      }
    }
  ],
  "recorded_outcome": {
    "status": "ok",
    "intent": "top_bottom_products"
  }
}
```

Поля:

- `responses` — **список** сырых ответов модели в порядке LLM-вызовов
  (`resolve_context` → `agent_decision` → `fix_dsl`). Каждый элемент — один из:
  - `{"tool_call": {"name": "<intent|clarify|refuse>", "arguments": {...}}}` —
    tool calling;
  - `{"content": "{\"tool\": \"...\", \"arguments\": {...}}"}` — JSON-адаптер
    для провайдера без tool calling (или ответ `fix_dsl`/`resolve_context`).
  Для кейса с починкой DSL второй элемент — ответ `fix_dsl`.
- `recorded_outcome` — снимок финального решения графа при записи:
  `status` (`ok|clarify|refuse|error|fallback`) и `intent` (или `null`).
  Replay сравнивает с ним результат воспроизведения; это не `expected_dsl`.
- `recorded_at`, `provider`, `model` — как и на какой модели записано.
- Обратная совместимость: старый одиночный `response` принимается как
  `responses` из одного элемента, но **recorded-кассеты пишутся только в
  `responses`**.

`source`:

- `recorded` — реальный ответ модели; только такие кассеты участвуют в eval;
- `synthetic_example` — пример формата, в метрику не попадает.

## Что хранит кассета, а что нет

- Хранит: сырой ответ модели и `recorded_outcome` (status/intent).
- Не хранит: `question`, `messages`, ключи/токены, `expected_dsl`/`expected_status`.

`expected_dsl`/`expected_status` — единственный источник ожиданий в
`tests/golden/cases.json`; кассеты их не дублируют. Если модель ошиблась —
ошибка фиксируется в кассете, а не подменяется ожидаемым DSL. Качество модели
измеряет live-прогон (`tools/eval_live.py`), а не replay.

## Replay vs live

- **Replay** (CI, без сети) — воспроизводит `recorded_outcome`: ловит регрессии
  парсера, Pydantic, `fix_dsl`, маршрутизации и статусов. Не оценивает модель.
- **Live** (`tools/eval_live.py`) — реальный граф на живой модели: DSL-точность
  и факт-чек по `expected_dsl`/`expected_status` из `cases.json`.

## Как записывать

1. Прогнать кейс через граф на эталонном провайдере (см. `tools/record_cassettes.py`).
2. Скрипт сохраняет **сырые** ответы модели и `recorded_outcome`, не подменяя их.
3. Указать provider/model/дату (скрипт делает это сам из настроек).
4. Не коммитить ключи, заголовки и сырой текст вопроса.
5. Существующие recorded-кассеты не перезаписываются без `--force`.
