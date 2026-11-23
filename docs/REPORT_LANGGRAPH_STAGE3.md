# Отчёт: этап 3 — нативные tool calls и аудит шагов графа

- **Проект:** `1c-ai-analyst` (NL2DSL: вопрос → JSON-DSL → Pydantic → pandas)
- **Дата:** 2026-11-18
- **База:** `c1a2de7` (этап 2.5a), `ddd473b` (этап 1.5)
- **ТЗ:** «Гибридная архитектура LangGraph», ревизия 6.4, §4.2–§4.4
- **ADR:** [0015](adr/0015-native-tools-audit.md)
- **Статус:** код и тесты готовы; `pytest -q` → **754 passed** (этап 3: 728 + исправления 3.1: +26). Коммит/push не выполнялись.

## 1. Итог

1. **Нативные tool calls для OpenAI-совместимых провайдеров.** `LLMClient`
   отправляет `tools` + `tool_choice="auto"`, разбирает `tool_calls` в
   `CompletionResult`. JSON-адаптер остаётся fallback'ом, если провайдер
   явно не поддерживает tools.
2. **Аудит шагов LangGraph.** Каждый узел графа пишет событие в
   `.mcp_state/audit.jsonl` через `AuditLog`; вопрос, ответ модели и
   credentials не логируются.
3. **Связка с лимитами.** `LLMCallGuard` по-прежнему считает вызовы;
   `llm_calls` попадает в аудит как счётчик, но лимит по журналу не считается.

## 2. Что изменено

| Файл | Изменение |
|---|---|
| `src/onec_analyst/llm_agent.py` | `LLMClient.supports_tools=True`; `_payload`/`_post_json`/`_completion_from_response`; native tools + JSON-fallback |
| `src/onec_analyst/agent/audit.py` | новый: `audit_event`, белый список полей |
| `src/onec_analyst/agent/state.py` | `AgentState.audit`, `initial_state(audit=...)` |
| `src/onec_analyst/agent/graph.py` | обёртка узлов, `graph_start/end/error`, `AuditLog` по умолчанию |
| `src/onec_analyst/dispatcher.py` | `process_request(..., audit=None)` |
| `tests/test_agent_audit.py` | 5 тестов аудита |
| `tests/test_gigachat.py` | native tools + fallback вместо старого теста «tools игнорируются» |

## 3. Native tools

`LLMClient.complete(messages, tools)`:

- с tools: `tools`, `tool_choice="auto"`, без `response_format`;
- `message.tool_calls` → `ToolCall(name, arguments)`;
- пустой ответ и битые аргументы → `LLMError`;
- при 400/404/422 с упоминанием tools/function — повтор без tools через
  `chat` (JSON-адаптер);
- без tools — прежний текстовый JSON-режим.

`supports_tools=True` означает, что `LLMPlanner` тоже использует нативный
путь; свободный текст без tool call в планировщике трактуется как уточнение,
в графе — как `handle_error`.

## 4. Аудит

События:

```text
graph_start
resolve_context
agent_decision
validate_ok | validate_fail
fix_dsl
clarify
refuse
handle_error
graph_end
graph_error
```

Поля: `status`, `intent`, `retry_count`, `llm_calls`, `missing`, `fallback`,
`provider`, `model`, `error_type`. `question`, `resolved_question`,
`raw_response`, `credentials` не пишутся.

`AuditLog.record` уже потокобезопасен и не роняет ответ при `OSError`;
`audit_event` дополнительно не пробрасывает исключения.

## 5. Проверка

```powershell
.\.venv\Scripts\python.exe -m pytest -q
# 754 passed
```

Новые тесты:

- `test_ok_path_writes_graph_decision_and_validation`;
- `test_clarify_and_refuse_are_audited`;
- `test_fix_path_audits_validate_fail_and_retry`;
- `test_audit_has_no_question_or_credentials`;
- `test_broken_audit_does_not_break_answer`;
- `test_llm_client_complete_parses_tool_calls_and_sends_tools`;
- `test_llm_client_falls_back_to_json_when_tools_unsupported`.

## 6. Отклонения и риски

1. **Fallback по тексту ошибки.** Признак «tools unsupported» — эвристика по
   400/404/422 и словам tools/function. При смене формулировок провайдера тест
   нужно обновлять.
2. **`LLMPlanner` и свободный текст.** При `supports_tools=True` свободный
   текст без tool call трактуется как уточнение. Для графа это не так: там
   свободный текст → `handle_error`.
3. **Аудит по умолчанию.** `run_agent_graph` создаёт `AuditLog()`, если журнал
   не передан; в тестах путь изолирован `ONEC_MCP_AUDIT_FILE`.
4. **Legacy `GigaChatClient`** не удалён; health-check/список моделей — за ним.
5. **Исторические счётчики** в README/RUNBOOK/UI_ACCEPTANCE не обновлялись;
   актуальные — `pytest` и `docs/INDEX.md` (754).

## 7. Исправления 3.1 (ревью этапа 3)

Точечная доработка по замечаниям ревью, без переписывания графа и контракта.

### 7.1 Сужение fallback на tools

- `LLMClient` откатывается к JSON-адаптеру только при 400/404/422 **и** явном
  маркере неподдержки tools (константа `_TOOLS_UNSUPPORTED_MARKERS` + функция
  `_tools_unsupported` в `src/onec_analyst/llm_agent.py`).
- Больше не маскируются: 500, сетевая ошибка/таймаут, 400 «invalid model»,
  422 про схему («Type properties…»).
- Падение fallback-вызова `chat` → `LLMError` без второго fallback.

### 7.2 Аудит по ТЗ §4.4

- В каждое событие пишется `step=event` (плюс `event` — для совместимости с MCP-аудитом).
- `duration_ms` (int мс): время узла в обёртке, общее время графа в `graph_end`.
- Отдельное событие `fallback` при `status="fallback"` (в дополнение к `handle_error`).
- `graph_end` пишет `status`, `intent`, `retry_count`, `llm_calls`, `fallback`, `error_type`.
- `error_type` — категория: `validation` (validate_fail/fix_dsl), `llm`
  (agent_decision), `unhandled` (handle_error), `error` — прочее.
- Исправлено определение `decision` в обёртке узла: ключ есть, но `None` ≠ ключа нет.

### 7.3 Документация

- ADR 0015: разделы «Соответствие ТЗ §4.4» и «LLMPlanner — legacy-путь».
- `LLMPlanner` зафиксирован как legacy: в проде планирование идёт через граф,
  его поведение с native tools в этом этапе не выравнивается.

### 7.4 Тесты

- `tests/test_llm_client_tools.py` — 22 негативных сценария `LLMClient`.
- `tests/test_agent_audit.py` — +4 теста (step/duration_ms, событие fallback,
  graph_end status/fallback/error_type, категории error_type).

```powershell
.\.venv\Scripts\python.exe -m pytest -q
# 754 passed
```

## 8. Что осталось на этап 4

- кассеты GoldSet: replay 50 кейсов в CI;
- live-прогон на эталонной модели;
- `eval_live.py`, `eval_judge.py`, `docs/EVAL.md`;
- связка `check_facts` с реальным `SummaryPayload` на всех кейсах;
- отчёт по категориям simple/context/edge_case/non_analytical.

## 9. Файлы

Добавлены:

```text
src/onec_analyst/agent/audit.py
tests/test_agent_audit.py
tests/test_llm_client_tools.py
docs/adr/0015-native-tools-audit.md
docs/REPORT_LANGGRAPH_STAGE3.md
```

Изменены:

```text
src/onec_analyst/llm_agent.py
src/onec_analyst/agent/{audit,state,graph}.py
src/onec_analyst/dispatcher.py
tests/test_gigachat.py
docs/{INDEX,ARCHITECTURE}.md
```
