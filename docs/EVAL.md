# EVAL: оценка агентного пути

Воспроизводимая оценка гибридного LangGraph-агента (вопрос → JSON-DSL → Pydantic
→ pandas). Слои разделены по смыслу и среде (ADR 0013 D5): кассеты в CI проверяют
**детерминированную часть**, live-прогон измеряет **качество модели**, judge —
мягкий сигнал по формулировкам. Ожидания — в одном месте, `tests/golden/cases.json`.

## 1. Три слоя оценки

| Слой | Что проверяет | Среда | Где |
|---|---|---|---|
| Replay кассет | парсер → Pydantic → `fix_dsl` → маршрутизация → статусы (воспроизводит `recorded_outcome`) | CI, без сети | `tests/test_golden_replay.py` |
| Expected-DSL | `pipeline.execute` + факт-чек на каноническом DSL (без LLM) | CI, без сети | `tests/test_golden_expected_dsl.py` |
| Live-прогон | DSL-точность и факт-чек модели относительно GoldSet | вручную/по расписанию | `tools/eval_live.py` |
| LLM-as-judge | формулировки ответа: числа, выводы, отказ, уточнение | вручную, не CI-гейт | `tools/eval_judge.py` |

`check_facts` (детерминированный факт-чек) используется и в expected-DSL, и в
live: он проверяет **все** факты кейса, ярлыки по тексту сводки и числа по
структурированному `SummaryPayload`.

## 2. Формат кассет

Кассета — сырой ответ модели на одном кейсе + снимок `recorded_outcome`
(status/intent). Файл `tests/golden/cassettes/<case_id>.json`:

```json
{
  "case_id": "test_001_top_simple",
  "source": "recorded",
  "recorded_at": "2026-11-18",
  "provider": "deepseek",
  "model": "deepseek-chat",
  "responses": [
    {"tool_call": {"name": "top_bottom_products", "arguments": {"limit": 5}}}
  ],
  "recorded_outcome": {"status": "ok", "intent": "top_bottom_products"}
}
```

- `responses` — список сырых ответов в порядке LLM-вызовов (`resolve_context` →
  `agent_decision` → `fix_dsl`): `{"tool_call": {...}}` или `{"content": "..."}`.
- `recorded_outcome` — что граф сделал при записи (не `expected_dsl`).
- Кассета не хранит question/messages/ключи. Ожидаемый DSL не дублируется.

### Как записывать

```powershell
python tools/record_cassettes.py                 # все 50, провайдер из .env
python tools/record_cassettes.py --case test_001_top_simple
python tools/record_cassettes.py --force         # перезаписать recorded
python tools/record_cassettes.py --provider gigachat --model GigaChat-2-Pro
```

Скрипт перехватывает сырой `CompletionResult` каждого LLM-вызова
(`RecordingClient`) и пишет только `responses` + `recorded_outcome`. Без доступа
к модели — останавливается, фейковых кассет не создаёт. Существующие recorded не
перезаписываются без `--force`.

## 3. Replay vs live

- **Replay** (`test_golden_replay.py`) проигрывает кассету через `CassetteClient`
  (без сети) и сверяет результат графа с `recorded_outcome`. Ловит регрессии
  парсера/Pydantic/`fix_dsl`/маршрутизации. **Не** сравнивает с
  `expected_dsl`/`expected_status`: модель могла ошибиться, и это фиксируется в
  кассете, а не превращается в красный CI.
- **Live** (`tools/eval_live.py`) гоняет реальный граф на живой модели и сверяет
  статус/DSL с `expected_status`/`expected_dsl`, а сводку — факт-чеком. Только он
  измеряет качество модели. Запускается вручную/по расписанию (нужен ключ и
  бюджет).

## 4. expected_facts: ярлыки + числа по payload

Факт-чек проверяет все факты кейса (`tests/golden/checks.py`):

- **Ярлыки** (сущность, метрика, период) — по тексту сводки;
- **Числа** (`"value"`) — по `SummaryPayload.values` (`item/metric/period/value`),
  а не парсингом отформатированной строки.

12 ok-кейсов содержат числовой `value`, посчитанный **независимым pandas-запросом**
по `tests/fixtures/golden_sales.csv` (не через `dispatch_intent`), чтобы факт-чек
не сравнивал расчёт с самим собой. Числа проверяются в `test_golden_expected_dsl.py`
и в live через `payload={"values": [...]}`.

## 5. Judge-рубрика и ограничения

15 кейсов (`JUDGE_SAMPLE` в `tools/eval_judge.py`), бинарные вопросы («да»/«нет»):

| Статус | Вопросы судьи |
|---|---|
| ok | есть ли в тексте числа, противоречащие переданным значениям расчёта? есть ли утверждения, противоречащие данным? |
| refuse | отказ объяснён понятно и по существу? |
| clarify | уточнение относится к `missing`? |

Вход судьи — **`SummaryPayload`** (посчитанные значения), а не сырой CSV витрины.
Рубрика — на **противоречие** (faithfulness), а не на полноту: отсутствующее в
payload число само по себе не противоречие, а производные величины (доли, счётчики,
first/last) сводка имеет право вычислять сверх payload. Сравнивать агрегаты с сырым
CSV — значит мерить арифметику LLM. Отчёт разделяет сигналы: refuse/clarify,
ok-числа, ok-выводы. Расширение `SummaryPayload` под производные поля — в бэклог
(меняет контракт и правила факт-чека, требует ADR).

Ограничения: судья — **отдельный провайдер** (`JUDGE_PROVIDER`/`JUDGE_MODEL`),
**не** через `on_llm_call`; не CI-гейт; бинарный ответ — грубый сигнал при
изменении промптов, а не абсолютная истина.

**Цепочка проверки:** факт-чек (ручные константы) — авторитет по числам;
judge — авторитет по формулировкам и честности отказов/уточнений. Слои не
дублируются: каждый меряет своё.

## 6. Команды запуска

```powershell
# CI (без сети)
.\.venv\Scripts\python.exe -m pytest -q tests/test_golden_replay.py tests/test_golden_expected_dsl.py

# live-прогон (нужен ключ)
.\.venv\Scripts\python.exe tools/eval_live.py
.\.venv\Scripts\python.exe tools/eval_live.py --provider gigachat --model GigaChat-2-Pro --label gigachat

# judge (отдельный провайдер)
.\.venv\Scripts\python.exe tools/eval_judge.py
.\.venv\Scripts\python.exe tools/eval_judge.py --judge-provider deepseek --judge-model deepseek-chat

# запись кассет
.\.venv\Scripts\python.exe tools/record_cassettes.py
```

`PYTHONPATH` не нужен при запуске из корня (скрипты добавляют `src` и корень сами).

## 7. Baseline

| Дата | Провайдер/модель | DSL % | Fact-check % | Judge % |
|---|---|---|---|---|
| 2026-11-18 | deepseek / deepseek-chat | 81.6 | 100.0 | — |
| 2026-11-18 | gigachat / GigaChat-2-Pro | 69.4 | 100.0 | — |
| 2026-11-18 | gigachat / GigaChat-2-Pro (judge) | — | — | 92.0 |
| 2026-11-19 | deepseek / deepseek-chat (S1) | 100.0 | 100.0 | — |
| 2026-11-19 | deepseek / deepseek-chat (judge, contradiction) | — | — | 96.0 |

Status accuracy: deepseek 89.8 %, gigachat 83.7 % (2026-11-18); после S1 — deepseek 100 %.
Детали — в `EVAL_LIVE*.md`, `EVAL_JUDGE.md` (+ JSON рядом).

**Judge history:** 52.0 % (сырой CSV — мерил арифметику LLM) → 80.0 % (payload,
рубрика на полноту) → 92.0 % (рубрика на противоречие) → **96.0 %** (метки периода
у экстремумов). Baseline = **96.0 %, 24/25**; единственный остаток — **007**: причина
судейская — payload содержит отрицательную дельту выпавшего (−165 000, корректные
данные), и судья читает её как «падение» при «падений — 0» в тексте. Дефект не в
системе: и данные, и сводка корректны — спотыкается интерпретация судьи.

## 8. Пороги

- **CI-гейт:** replay — 100 % совпадение с `recorded_outcome` (без регрессии > 3 п.п.);
  expected-DSL — 31/31 факт-чек.
- **Live (ориентиры):** overall ≥ 85 %, simple ≥ 90 %, context ≥ 75 %,
  edge_case/non_analytical — 100 % корректного статуса.
- **Факт-чек:** 100 % по ярлыкам и 100 % по числам на фикстуре.
- **Judge:** ≥ 90 % «да» — сигнал, не гейт.

Цифры — ориентиры; первый live-прогон фиксирует baseline, дальше — non-regression
(ADR 0013 D5).
