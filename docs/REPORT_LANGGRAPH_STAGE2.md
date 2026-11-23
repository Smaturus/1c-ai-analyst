# Отчёт: этап 2 — LangGraph-агент как planning-путь при `use_llm=True`

- **Проект:** `1c-ai-analyst` (NL2DSL: вопрос → JSON-DSL → Pydantic → pandas)
- **Дата:** 2026-11-18
- **Базовые коммиты:** `ca687c7` (ADR 0013 + GoldSet 6.4), `2d8f32f` (dispatcher/pipeline/summary/session)
- **ТЗ:** «Гибридная архитектура LangGraph», ревизия 6.4 (§1–§5)
- **ADR:** [0013](adr/0013-langgraph-hybrid-contract.md)
- **Статус:** код и тесты готовы; `pytest -q` → **683 passed**. Коммит/push не выполнялись (по договорённости).

> Документ создан по прямому запросу владельца проекта, чтобы отчёт этапа лежал рядом с кодом.
> Дневниковые записи по-прежнему ведутся отдельно в `docs/history/` и не публикуются.

---

## 1. Итог

Добавлен альтернативный planning-путь: при `use_llm=True` вопрос проходит через
LangGraph-граф (один `agent_decision` с tool calling, явные статусы, мета-инструменты,
память, одна починка DSL). Офлайн-путь `use_llm=False` не изменился: граф не
вызывается вообще, решение даёт `RuleBasedPlanner`, исполняет общий пайплайн.

Контракт 7 интентов, Pydantic-модели, `analytics.py` и тексты сводки не менялись.
`AgentDecision` расширен аддитивно: `status`, `refusal_reason`, `trace`.

---

## 2. Baseline и рабочее дерево

- Baseline до изменений: **560 passed** (`.\.venv\Scripts\python.exe -m pytest -q`).
- Первый прогон под ограниченной песочницей дал `540 passed, 20 errors` — ошибки
  только `PermissionError` при удалении временных каталогов (ограничение окружения,
  не тесты). После переключения сессии на full access baseline воспроизвёлся: 560.
- В рабочем дереве **до начала** уже были незакоммиченные изменения stage 1.5:
  `src/onec_analyst/summary.py`, `tests/golden/README.md`, `tests/golden/checks.py`,
  `tests/test_golden_facts.py`, `tests/test_summary_payload.py`, новый
  `tests/test_golden_expected_dsl.py`. Они не откатывались и не переписывались;
  `git reset/checkout` не выполнялись.
- Установлен `langgraph 1.2.22` в `.venv` (диапазон `>=0.2,<2`); Docker/CI — этап 5.

---

## 3. Что добавлено

### 3.1. Пакет `src/onec_analyst/agent/`

| Файл | Содержимое |
|---|---|
| [state.py](../src/onec_analyst/agent/state.py) | `AgentState` по ТЗ 2.1, `DecisionStatus`, `LLMCallGuard`, `CallBudgetGuard`, `UnlimitedGuard`, `initial_state`, `status_of` |
| [tools.py](../src/onec_analyst/agent/tools.py) | 7 бизнес-инструментов из `build_tools()` + `clarify`/`refuse`; разбор ровно одного вызова; JSON-адаптер |
| [prompts.py](../src/onec_analyst/agent/prompts.py) | `agent_decision`, `resolve_context`, `fix_dsl`; подписи инструментов генерируются из Pydantic-схем |
| [nodes.py](../src/onec_analyst/agent/nodes.py) | `resolve_context`, `agent_decision`, `validate_dsl`, `fix_dsl`, `clarify`, `refuse`, `handle_error` |
| [graph.py](../src/onec_analyst/agent/graph.py) | `StateGraph`, маршрутизация, `run_agent_graph`, recursion limit, trace |
| [__init__.py](../src/onec_analyst/agent/__init__.py) | ленивый реэкспорт `run_agent_graph` |

### 3.2. Изменённые модули

| Файл | Изменение |
|---|---|
| [llm_agent.py](../src/onec_analyst/llm_agent.py) | `AgentDecision.status/refusal_reason/trace` (аддитивно); `status="clarify"` у уточнений правил и `_decision_from_plan` |
| [pipeline.py](../src/onec_analyst/pipeline.py) | `status="clarify"` у `validate_decision` для `ClarificationPlan` |
| [dispatcher.py](../src/onec_analyst/dispatcher.py) | `process_request(..., llm_guard=None)`; агентная ветка; `AssistantResult.status/refusal_reason`; нормализация `status=error` |
| [app.py](../app.py), `app_local.py` | плашка отказа, статус в «Технических деталях» |
| [pyproject.toml](../pyproject.toml), [requirements.txt](../requirements.txt) | `langgraph>=0.2,<2` |

`app_local.py` синхронизирован вручную (тот же core), локальная кнопка обновления из 1С
не тронута; файл остаётся вне git (`.git/info/exclude`).

### 3.3. Новые тесты

| Файл | Тестов | Что проверяет |
|---|---:|---|
| [test_agent_state.py](../tests/test_agent_state.py) | 17 | статусы, сериализация состояния, аддитивность `AgentDecision`, guard |
| [test_agent_tools.py](../tests/test_agent_tools.py) | 16 | 7+2 инструмента, схемы из Pydantic, ровно один вызов, JSON-адаптер |
| [test_agent_nodes.py](../tests/test_agent_nodes.py) | 39 | триггеры/наследование, validate, fix, clarify rounds, refuse, MVP-политика |
| [test_agent_graph.py](../tests/test_agent_graph.py) | 31 | маршрутизация, 0/2 tool call, free text, invalid → fix → ok, лимит, AppTest |
| [test_agent_memory.py](../tests/test_agent_memory.py) | 8 | 009/016 → clarify(period2); 024/048 → наследование; 008/029 → refuse |
| [test_agent_isolation.py](../tests/test_agent_isolation.py) | 4 | `agent/*` без Streamlit; в офлайне граф не вызывается |
| [test_agent_parity.py](../tests/test_agent_parity.py) | 8 | простые вопросы: intent+params как у `RuleBasedPlanner` |
| [agent_support.py](../tests/agent_support.py) | — | fake-клиент без сети и заготовки ответов |

Итого 123 новых теста: **560 → 683 passed**.

---

## 4. Поток выполнения

```text
process_request(use_llm=True)
  └─ dispatcher: planner = select_planner(...)
       └─ isinstance(planner, LLMPlanner) → run_agent_graph(state)

START
  ├─ (previous_dsl и триггер) → resolve_context → agent_decision
  └─ иначе                    → agent_decision
agent_decision ──status=ok──────────────→ validate_dsl
               ├─status=clarify─────────→ clarify   → END
               ├─status=refuse──────────→ refuse    → END
               └─status=error/нет решения→ handle_error → END
validate_dsl ──валидно──────────────────→ END
             ├─невалидно, retry=0───────→ fix_dsl → validate_dsl
             └─невалидно, retry>=1──────→ handle_error → END
```

Расчёт остаётся в общем пайплайне: `dispatcher` после `status=ok/fallback` вызывает
`pipeline.execute` (`dispatch_intent` → DataFrame → `SummaryPayload`). Граф сам
DataFrame не строит.

---

## 5. Контракт решения

`AgentDecision` (аддитивные поля, старые конструкторы не ломаются):

```text
ok: bool
intent: str | None
params: dict
clarification: ClarificationPlan | None
error: str | None
raw_response: str
dsl: dict
source: "llm" | "rules"
status: ok | clarify | refuse | error | fallback     # новое
refusal_reason: str | None                            # новое
trace: tuple[str, ...]                                # новое
```

- `clarify` — не исполняется: `AssistantResult.result is None`, UI рисует `st.info` и pills.
- `refuse` — не исполняется, причина обязательна, UI рисует отдельную плашку.
- `fallback` — решение правил, исполняется как обычно; `source="rules"`.
- `error` — без DataFrame, UI показывает существующую ошибку.

---

## 6. Лимиты LLM

- Протокол `LLMCallGuard`: `before_call() -> bool`, `after_success()`.
- Проверка перед `resolve_context` (только LLM-fallback), `agent_decision`, `fix_dsl`;
  `after_success` — только после успешного ответа провайдера.
- `initial_state` по умолчанию даёт `CallBudgetGuard(3)`: один запрос не делает больше
  трёх вызовов даже без лимитера UI. UI-лимитер сессии/дня не менялся.
- При исчерпании — ни одного частичного вызова, уход в `handle_error` → `fallback`
  (правила) или `error`.

---

## 7. Память и период

- `previous_dsl` и `result_entities` передаются из `ConversationState` в состояние графа.
- `resolve_context` включается только при триггерах («они/эти/те же/а теперь/то же
  самое/за тот же период»), детерминированно наследует даты/фильтры/гранулярность и
  подставляет сущности в текст вопроса. LLM-fallback — только если правила не справились.
- `period2` обязателен для `compare_top_products`, `counterparty_dynamics`,
  `group_dynamics`: при одном периоде — `clarify(missing=["period2"])`, без подстановки.
- Счётчик `clarification_rounds` остаётся один: владелец — `ConversationState`/UI;
  граф читает значение и после двух кругов делает `force_interpretation`.

---

## 8. Отклонения и принятые решения

1. **Детерминированная MVP-политика отказа** поверх выбора модели. Кейсы 008/029/033/023,
   средний чек, `revenue_vat`, `products`, `product_filter` у counterparty и будущий
   период получают `refuse(reason)` независимо от того, что вернула модель. Это
   гарантирует DoD и GoldSet; «тихих подстановок» нет, отказ объяснён.
2. **Наследование периода** пишется в текст промпта и применяется к аргументам только
   если модель их не передала; `period2` не наследуется никогда.
3. **Граф включается только для реального `LLMPlanner`**, а не по одному флагу `use_llm`:
   это сохранило совместимость существующих тестов `select_planner` и гарантирует,
   что при ненастроенной LLM остаются правила.
4. **Офлайн-ошибки получили явный `status="error"`** (раньше `status` оставался `ok`);
   поведение UI не изменилось, статусы стали различимыми.
5. **`app_local.py` синхронизирован** с `app.py` по UI-части; файл не добавлен в git.
6. **Ложное срабатывание «НДС»** исправлено: «выручка без НДС» — штатный запрос,
   отказ только для «с НДС»/`revenue_vat` (есть тест).

---

## 9. Проверка

```powershell
.\.venv\Scripts\python.exe -m pytest -q
# 683 passed
```

Целевые прогоны:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_agent_state.py tests/test_agent_tools.py `
    tests/test_agent_nodes.py tests/test_agent_graph.py tests/test_agent_memory.py `
    tests/test_agent_parity.py tests/test_agent_isolation.py
# 133 passed (включая AppTest LLM-пути)
```

Соответствие Definition of Done:

- [x] 560 + новые тесты, все passed (683).
- [x] `use_llm=False` граф не вызывается; `use_llm=True` — ровно один запуск.
- [x] `ok/clarify/refuse/error/fallback` различаются; clarify и refuse не исполняются.
- [x] `fix_dsl` — максимум один раз.
- [x] 0/2 tool call и свободный текст → `handle_error`.
- [x] 009/016 → `clarify(period2)`; 024/048 наследуют период; 008/029 → refuse.
- [x] Лимит → `fallback`, без падения.
- [x] `agent/*` не импортирует Streamlit (AST + импорт в чистом процессе).
- [x] Простые вопросы дают тот же intent/params, что `RuleBasedPlanner`.

---

## 10. Что осталось

- **Этап 3:** нативные tools для OpenAI-провайдеров; `audit.jsonl` по шагам графа
  (`graph_start`, `resolve_context`, `agent_decision`, `validate_ok/fail`, `fix_dsl`,
  `clarify`, `refuse`, `fallback`, `handle_error`, `graph_end`) без сырого вопроса;
  полный аудит каждого вызова.
- **Этап 4:** нормализованный GoldSet, фикстура, кассеты replay, `check_facts`,
  `eval_live.py`, `eval_judge.py`, `docs/EVAL.md`.
- **Этап 5:** ruff/mypy, AppTest-расширение, Docker/CI, обновление README/INDEX
  (счётчики тестов), диаграмма, версия 0.8.0.

---

## 11. Файлы этапа

Добавлены:

```text
src/onec_analyst/agent/{__init__,state,tools,prompts,nodes,graph}.py
tests/{agent_support,test_agent_state,test_agent_tools,test_agent_nodes,
       test_agent_graph,test_agent_memory,test_agent_isolation,test_agent_parity}.py
docs/REPORT_LANGGRAPH_STAGE2.md
```

Изменены:

```text
pyproject.toml, requirements.txt
src/onec_analyst/{llm_agent,pipeline,dispatcher}.py
app.py, app_local.py (app_local.py — вне git)
docs/INDEX.md
```

Не менялись: `analytics.py`, ТЗ, ADR 0013, GoldSet, диаграмма.
