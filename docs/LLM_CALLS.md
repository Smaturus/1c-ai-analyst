# LLM-вызовы: карта, лимиты и аудит

Документ описывает **все места, где проект обращается к языковой модели**, сколько
вызовов делает один запрос, как это учитывается в лимитах и что попадает в аудит.

Актуально для состояния после этапа 5 (включая отложенный этап 3.2). Правило
ТЗ 6.4 §4.2 — 15 успешных вызовов на сессию и 100 в день — **реализовано
полностью**: лимит считается на каждый вызов (см. §5).

## 1. Провайдеры и клиенты

| Провайдер | Клиент | Где используется |
|---|---|---|
| GigaChat | `LangChainGigaChatClient` (`agent/llm.py`) | Граф: `resolve_context`, `agent_decision`, `fix_dsl` |
| GigaChat | `GigaChatClient` (legacy, `llm_agent.py`) | Health-check, список моделей, классификатор уточнений, `LLMPlanner` вне графа |
| OpenAI / DeepSeek / Ollama | `LLMClient` (`llm_agent.py`) | Граф, `LLMPlanner` вне графа, классификатор уточнений |
| Любой | `make_agent_client` | Выбор клиента для графа: GigaChat → LangChain, остальные → `LLMClient` |

Модель всегда берётся из `settings.model`:

- GigaChat: `GigaChat(model=settings.model)`;
- OpenAI-совместимые: `payload["model"] = settings.model`.

Список моделей GigaChat строится отдельно через `GET /v1/models` legacy-клиентом;
это **не** вызов генерации.

## 2. Карта вызовов

| № | Точка | Файл / функция | Когда вызывается | Tools | Макс. на запрос |
|---|---|---|---|---|---|
| 1 | `resolve_context` | `agent/nodes.py` | Только если есть `previous_dsl`, триггеры контекста и детерминированное наследование не восстановило период | Нет | 1 |
| 2 | `agent_decision` | `agent/nodes.py` | Всегда в агентном пути (`use_llm=True`), если не исчерпан guard | 7 бизнес-инструментов + `clarify`/`refuse` | 1 |
| 3 | `fix_dsl` | `agent/nodes.py` | Только после ошибки валидации DSL; не более одного раза | Нет | 1 |
| 4 | Классификатор уточнений | `clarification.classify_with_llm` | Только в UI, если включена LLM и выбранный вариант уточнения спорный | Нет | 1 |
| 5 | `LLMPlanner.plan` | `llm_agent.py` | **Legacy/вне production-графа**: прямые вызовы, smoke, тесты. В диспетчере вместо него граф | Зависит от клиента | 1 + 1 repair |
| 6 | Health-check | `check_provider` / `GigaChatClient.models` | `--check`, список моделей | Нет | 0 (генерации нет) |
| 7 | Eval | `tools/eval_live.py`, `tools/eval_judge.py` | Этап 4: вручную/по расписанию, не CI, не production | Зависит | 50 кейсов / 15 judge |

`agent_decision` — единственный **обязательный** вызов в графе. Остальные —
условные.

## 3. Сколько вызовов делает один запрос

| Сценарий | Вызовов LLM |
|---|---:|
| `use_llm=False` | 0 |
| Обычный вопрос без контекста, валидный DSL | 1 (`agent_decision`) |
| Контекст восстановлен детерминированно | 1 |
| Контекст через LLM-fallback | 2 (`resolve_context` + `agent_decision`) |
| Невалидный DSL | 2 (`agent_decision` + `fix_dsl`) |
| Контекст через LLM + невалидный DSL | 3 (максимум) |
| `clarify` / `refuse` от `agent_decision` | 1 |
| Лимит/сеть до вызова | 0–1 |
| Уточнение в UI (классификатор) | +1 отдельно, до основного запроса |

`CallBudgetGuard(3)` в составе `CompositeGuard` — жёсткий потолок на **один**
запрос. Это не «всегда 3»; типичный успешный запрос — 1 вызов.

## 4. Логический вызов vs HTTP-запрос

Один логический вызов может дать несколько HTTP-запросов:

- `LLMClient` при неподдержке tools: первый запрос с `tools`, затем fallback
  без tools. Это **один** логический вызов, но два HTTP-запроса.
- GigaChat SDK может повторить запрос при 401/сетевой ошибке — это тот же
  логический вызов.
- `check_provider` делает OAuth + `GET /models`; генерации нет.

В лимитах и `llm_calls` считается логический вызов (успешный ответ модели),
а не каждый HTTP-запрос.

## 5. Лимиты: требование ТЗ и фактическое поведение

### Требование ТЗ 6.4 §4.2

- 15 **успешных вызовов** LLM на сессию;
- 100 **вызовов** в день глобально;
- учёт на каждый вызов: `resolve_context` (если был), `agent_decision`, `fix_dsl`;
- проверка и инкремент атомарны;
- при исчерпании — никаких частичных вызовов, fallback (правила) или `handle_error`;
- инкремент — после успешного ответа провайдера; сетевые ошибки лимит не съедают.

### Что фактически сейчас (после этапа 3.2)

| Требование | Факт |
|---|---|
| 15 вызовов на сессию | `RateLimiterGuard` считает **каждый успешный вызов** (см. §3) |
| 100 вызовов в день | глобальный счётчик — тоже по вызовам |
| Учёт каждого узла | `resolve_context`, `agent_decision`, `fix_dsl` — каждый резервирует слот |
| Атомарность | `RateLimiter.try_consume` — проверка + инкремент в одной транзакции `BEGIN IMMEDIATE` |
| Потолок на запрос | `CompositeGuard(CallBudgetGuard(3), RateLimiterGuard)` — максимум 3 вызова |
| Сетевой сбой | `after_failure` возвращает резерв (`RateLimiter.release`); лимит не съедается |
| Классификатор уточнений | отдельный атомарный резерв через `try_consume` при `source=llm` |

Следствие: «N/15» в сайдбаре — это N успешных вызовов API (не запросов), и
фактических обращений к API ровно столько же, сколько показывает счётчик.

### Реализация (этап 3.2)

1. `RateLimiter.try_consume(session_id)` — атомарная проверка + резерв в одной
   транзакции SQLite (`BEGIN IMMEDIATE`), с учётом session/global.
2. `RateLimiterGuard` с `before_call()` / `after_success()` / `after_failure()`;
   `RateLimiter.release` снимает резерв при сбое.
3. `CompositeGuard` в `agent/state.py` объединяет `CallBudgetGuard(3)` (потолок
   на запрос) и `RateLimiterGuard` (лимит сессии/дня).
4. Проброс guard: `app.py` создаёт композитный guard и передаёт его в
   `process_request(llm_guard=...)` → `run_agent_graph`.
5. Тесты: `tests/test_rate_limiter_guard.py` — 3 вызова = +3; 16-й не уходит в
   API (fallback); сетевой сбой не съедает; глобальные 100; AppTest-счётчик.

## 6. Аудит

Каждый шаг графа пишется в `.mcp_state/audit.jsonl` (или `ONEC_MCP_AUDIT_FILE`)
через `AuditLog`:

```text
graph_start
resolve_context
agent_decision
validate_ok / validate_fail
fix_dsl
clarify
refuse
fallback
handle_error
graph_end
graph_error
```

Поля: `ts`, `event`, `step`, `status`, `intent`, `retry_count`, `llm_calls`,
`duration_ms`, `missing`, `fallback`, `provider`, `model`, `error_type`.

Не пишутся: `question`, `resolved_question`, текст ответа модели, `credentials`,
`Authorization`.

`llm_calls` в событиях — фактическое число успешных вызовов узлов; `graph_end`
содержит итог. Это не заменяет `RateLimiter`: аудит только фиксирует, лимит
считает guard.

## 7. Ошибки и fallback

| Ситуация | Поведение |
|---|---|
| Лимит исчерпан до вызова | `handle_error` → `fallback` (правила) или `error`; частичных вызовов нет |
| Сеть/таймаут | `LLMError` → `handle_error` → `fallback`/`error`; лимит не тратится |
| Провайдер не поддерживает tools (400/404/422 + явный маркер) | Один логический вызов: запрос без tools через JSON-адаптер |
| 500, `invalid model`, 422 про схему | `LLMError`, без fallback |
| Модель GigaChat недоступна | `LLMError` с именем модели → `handle_error`/`fallback`; модель не подменяется |
| Невалидный tool call / args | `validate_dsl` → `fix_dsl` (1 раз) → `handle_error` |
| Свободный текст без tool call | `handle_error` в графе; в `LLMPlanner` — уточнение (legacy) |

## 8. Как измерить

- `pytest -q` — **875 passed, 1 skipped** (на момент этапа 5).
- `tests/test_agent_audit.py` — события, `llm_calls`, отсутствие вопроса/секретов.
- `tests/test_llm_client_tools.py` — native tools, fallback, негативные сценарии.
- `tests/test_rate_limiter_guard.py` — per-call лимиты, атомарность, fallback.
- `AssistantResult.llm_success` — был ли успешный вызов LLM в запросе.
- `AgentState.llm_calls` — сколько успешных вызовов сделал граф.
- `audit.jsonl` — шаги, `llm_calls`, длительности.
- UI «Использовано вызовов ИИ: N/15» — N успешных вызовов (per-call, §5).

## 9. Ссылки

- [ADR 0013](adr/0013-langgraph-hybrid-contract.md) — граф, статусы, eval.
- [ADR 0014](adr/0014-gigachat-langchain.md) — GigaChat через `langchain-gigachat`.
- [ADR 0015](adr/0015-native-tools-audit.md) — native tools и аудит.
- [ARCHITECTURE.md](ARCHITECTURE.md) — компоненты и LLM-путь.
