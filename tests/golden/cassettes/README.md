# Кассеты GoldSet

Кассета — записанный ответ модели на одном кейсе, приведённый к уровню
`AgentDecision` и не зависящий от провайдера. CI проигрывает кассеты без сети:
парсер → Pydantic → `fix_dsl` → маршрутизация → факт-чек.

## Формат файла

Имя файла: `<case_id>.json` (например, `test_001_top_simple.json`).

```json
{
  "case_id": "test_001_top_simple",
  "source": "recorded",
  "recorded_at": "2026-11-18",
  "provider": "deepseek",
  "model": "deepseek-chat",
  "response": {
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
  },
  "notes": "необязательно"
}
```

Допустимые ответы:

- `{"tool_call": {"name": "<intent>", "arguments": {...}}}` — tool calling;
- `{"tool_call": {"name": "clarify", "arguments": {"missing": [...], "question": "..."}}}`;
- `{"tool_call": {"name": "refuse", "arguments": {"reason": "..."}}}`;
- `{"content": "```json ... ```"}` — JSON-адаптер для провайдера без tools.

`source`:

- `recorded` — реальный ответ модели; только такие кассеты участвуют в eval;
- `synthetic_example` — пример формата, в метрику не попадает.

## Как записывать

1. Прогнать кейс через `agent_decision` на эталонном провайдере.
2. Сохранить **сырой** ответ (tool call или content), не подменяя его ожидаемым
   DSL.
3. Указать provider/model/дату.
4. Не коммитить ключи, заголовки и сырой текст вопроса.

## Важно

Пока записан только синтетический пример `test_001_top_simple.json`. Это
каркас: replay-тест не должен выдавать его за качество модели. Реальные
кассеты появятся на этапе 4, после реализации графа.
