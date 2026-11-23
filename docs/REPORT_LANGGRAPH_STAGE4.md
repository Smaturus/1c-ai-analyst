# Отчёт этапа 4: eval-слой (кассеты, replay, live, judge)

Дата: 2026-11-18. Ветка `main`, HEAD `1092b20` (этап 3.1) — контракт 7 интентов,
граф, промпты и статусы **не менялись**. Добавлен только eval-слой.

## Что сделано

1. **Кассеты на уровне `AgentDecision`** — 50 recorded-кассет
   (`tests/golden/cassettes/`) на deepseek/deepseek-chat: сырые ответы модели
   (`responses`: tool call или JSON-контент) + снимок `recorded_outcome`
   (status/intent). Без question/messages/ключей; единственный источник ожиданий —
   `cases.json`.
2. **`CassetteClient`** (`tests/golden/cassette_client.py`) — проигрывает кассеты
   без сети: `complete(messages, tools) -> CompletionResult`, `supports_tools`,
   `LLMError` при исчерпании. Обратная совместимость с одиночным `response`
   (синтетический пример — `_example_synthetic.json`, в метрику не входит).
3. **`tools/record_cassettes.py`** — запись кассет на живой модели с
   `RecordingClient` (перехват сырых `CompletionResult`), `--force`, честный стоп
   без доступа.
4. **`tests/test_golden_replay.py`** — replay в CI: воспроизводит
   `recorded_outcome` (не `expected_dsl`), для ok — `pipeline.execute` +
   факт-чек; 033 — skip по xfail. Сети нет.
5. **Числовые факты** — 12 ok-кейсов получили `value` (независимый pandas-запрос
   по фикстуре, не через `dispatch_intent`); числа проверяются через
   `SummaryPayload`.
6. **`tools/eval_live.py`** — live-прогон 50 кейсов (`CallBudgetGuard(3)`), DSL-
   и факт-чек с разбивкой по категориям; отчёт Markdown + JSON.
7. **`tools/eval_judge.py`** — LLM-as-judge на 15 кейсах (`JUDGE_SAMPLE`),
   бинарная рубрика, отдельный провайдер (GigaChat-2-Pro), не через
   `on_llm_call`; не CI-гейт.
8. **Документация** — `docs/EVAL.md`, `EVAL_LIVE_*.md/json`, `EVAL_JUDGE.md/json`,
   обновлён `docs/INDEX.md`.

## Результаты

### Replay (CI, без сети)

- `tests/test_golden_replay.py` + `test_golden_cassettes.py` + expected-DSL +
  dataset: **зелёные**. Replay воспроизводит `recorded_outcome` для всех 49 кейсов
  (033 — skip). Replay **не** наказывает за ошибки модели.

### Live-прогон (DSL-точность и факт-чек)

| Провайдер/модель | Status % | DSL % | Fact-check % |
|---|---|---|---|
| deepseek / deepseek-chat | 89.8 | 81.6 | 100.0 |
| gigachat / GigaChat-2-Pro | 83.7 | 69.4 | 100.0 |

По категориям (DSL %):

| Категория | deepseek | gigachat |
|---|---|---|
| simple | 80.0 | 68.0 |
| context | 71.4 | 71.4 |
| edge_case | 81.8 | 54.5 |
| non_analytical | 100.0 | 100.0 |

Ориентиры (overall ≥ 85 %, simple ≥ 90 %, context ≥ 75 %) **не достигнуты** ни на
одной модели — это честный baseline, а не регрессия пайплайна.

### LLM-as-judge (GigaChat-2-Pro)

- Доля «да»: **92.0 %** (23/25) — выше сигнального порога 90 %.
- Два «нет»: `test_007_counterparty` (выводы), `test_014_filter_product` (числа) —
  мягкий сигнал, проверяется детерминированным факт-чеком (он прошёл).

## Где модель ошибается (зафиксировано, GoldSet не менялся)

DeepSeek — 9 расхождений: статус `009`/`016`/`023`/`044`/`045`, параметры
`002`/`007`/`014`/`017`. GigaChat — 15 расхождений, хуже на clarify-кейсах
(`010`/`011`/`020`/`042`/`043` → угадывает интент вместо уточнения) и edge_case.
Обе модели путают `ascending` (антитоп, `002`), параметры
`counterparty_dynamics` (`007`), `product_filter` у `timeseries` (`014`) и
относительные даты (`017`).

## Ограничения

- Кассеты записаны только на deepseek (provider-independent); GigaChat — второй
  live-прогон, кассеты не перезаписывал.
- Live/judge требуют ключ и бюджет — запускаются вручную, не в CI.
- Judge — бинарный и грубый: сигнал, не абсолютная истина.
- 033 (`revenue_vat`) — xfail/skip, вне MVP.

## Что осталось на этап 5

1. Довести DSL-точность до порогов (промпты/примеры, особенно clarify и
   edge_case) — с non-regression по live-метрикам.
2. Разобрать 4–7 систематических параметрических ошибок (`ascending`,
   `product_filter`, относительные даты).
3. По расписанию повторять live + judge, следить за baseline-таблицей.
4. (Опционально) поднять факт-чек на сравнение с `dispatch_intent` на расширенной
   фикстуре.
