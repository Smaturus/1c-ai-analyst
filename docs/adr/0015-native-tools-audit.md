# ADR 0015: Нативные tool calls у OpenAI-совместимых провайдеров и аудит шагов графа

- **Статус:** принято
- **Дата:** 2026-11-18
- **Этап:** 3
- **Связанные решения:** [ADR 0013](0013-langgraph-hybrid-contract.md) (граф и статусы), [ADR 0014](0014-gigachat-langchain.md) (GigaChat через `langchain-gigachat`)
- **Связанные документы:** ТЗ «Гибридная архитектура LangGraph» ревизия 6.4, §4.2–§4.4; `docs/REPORT_LANGGRAPH_STAGE3.md`
- **Затрагивает:** `src/onec_analyst/llm_agent.py`, `src/onec_analyst/agent/{audit,state,graph}.py`, `src/onec_analyst/dispatcher.py`, тесты

## Контекст

К этапу 2.5 GigaChat в графе уже использует нативный tool calling через
`langchain-gigachat`, а OpenAI-совместимые провайдеры (OpenAI, DeepSeek,
Ollama) по-прежнему отвечали текстом и разбирались JSON-адаптером. ТЗ 6.4 §1
требует нативного tool calling как основного пути, JSON-адаптер — как
равноправный fallback для провайдеров без tools. Кроме того, §4.4 требует
писать каждый шаг графа в `audit.jsonl` без сырого вопроса и секретов.

## Решения

### D1. `LLMClient` отправляет tools и разбирает `tool_calls`

**Решение.** `LLMClient.supports_tools = True`. При `tools` клиент отправляет
`tools` и `tool_choice="auto"` в Chat Completions, не добавляет
`response_format` и нормализует `message.tool_calls` в
`CompletionResult(content, tool_calls)`. Без `tools` поведение прежнее:
текстовый JSON-режим.

**Обоснование.** Это убирает расхождение «GigaChat умеет tools, остальные —
нет» и позволяет графу не знать провайдера. Схемы инструментов по-прежнему
берутся из `build_tools()`/Pydantic, контракт не меняется.

### D2. Fallback на JSON, если провайдер не поддерживает tools

**Решение.** `complete` откатывается к текстовому JSON-адаптеру только при
одновременном выполнении двух условий (ТЗ 6.4 §4.2): HTTP-статус 400/404/422
**и** текст ошибки содержит явный маркер неподдержки tools
(`_TOOLS_UNSUPPORTED_MARKERS`, например `tools are not supported` /
`unknown field 'tools'` / `unrecognized request argument: tools`). Остальное —
500, сетевая ошибка/таймаут, 400 «invalid model», 422 про схему
(«Type properties…») — пробрасывается наружу как `LLMError`, без «тихой»
подмены ответа. Если fallback-вызов `chat` тоже падает, наружу уходит
`LLMError` без второго fallback.

**Обоснование.** Широкий признак «(tool или function) и (400/404/422 или
unsupported/unknown)» маскировал реальные ошибки: 500 «unknown function» и
422 про схему уходили в JSON-режим. Полный маркер + белый список статусов
оставляют fallback только для реальной неподдержки tools.

### D3. Аудит шагов графа — через `AuditLog`

**Решение.** `AgentState.audit` хранит `AuditLog`; `agent/audit.py` пишет
события с белым списком полей (`status`, `intent`, `retry_count`,
`llm_calls`, `missing`, `fallback`, `provider`, `model`, `error_type`,
`step`, `duration_ms`). `graph.py` оборачивает узлы и пишет:

`graph_start`, `resolve_context`, `agent_decision`,
`validate_ok`/`validate_fail`, `fix_dsl`, `clarify`, `refuse`, `fallback`,
`handle_error`, `graph_end`; при сбое графа — `graph_error`.

Сырой вопрос, `resolved_question`, ответ модели и секреты не пишутся.
`AuditLog.record` уже потокобезопасен и не роняет ответ при `OSError`;
`audit_event` дополнительно не пробрасывает исключения.

**Обоснование.** Аудит — побочный канал: он не должен менять решение и не
должен содержать ПДн/коммерческую тайну. Белый список полей защищает от
случайного расширения логов в будущем.

### D4. Аудит и лимиты не дублируют друг друга

**Решение.** `LLMCallGuard` отвечает за бюджет вызовов (`before_call`/
`after_success`), `AuditLog` — за журнал шагов. `llm_calls` попадает в события
как счётчик, но лимит по аудиту не считается.

**Обоснование.** Разные зоны ответственности: guard может отменить вызов,
аудит только фиксирует факт; смешивание усложнило бы тесты и fallback.

### D5. Соответствие ТЗ §4.4

Схема событий и полей выровнена с ТЗ 6.4 §4.4 (исправления этапа 3.1):

- **`step` vs `event`.** В каждое событие пишется и `step=event` (по ТЗ), и
  `event` (совместимость с MCP-аудитом, который фильтрует по `event`).
- **`duration_ms`.** Обёртка узла пишет время выполнения узла (int мс);
  `graph_end` — общее время графа; `graph_error` — время до сбоя.
- **`fallback`.** Отдельное событие `fallback` при `status="fallback"`
  (в дополнение к `handle_error`); в `graph_end` `fallback` остаётся bool-полем.
- **`error_type`.** Категория, а не литерал `"error"`: `validation`
  (validate_fail/fix_dsl), `llm` (agent_decision), `unhandled` (handle_error),
  `error` — прочее.

### D6. `LLMPlanner` — legacy-путь

`LLMPlanner` остаётся legacy-планировщиком: в проде планирование идёт через
граф (`run_agent_graph`), а `LLMPlanner` используется только health-check'ами
и офлайн-сценариями. Хотя он теперь тоже шлёт native tools (через
`LLMClient.supports_tools`), его поведение с native tools в этом этапе
**намеренно не выравнивается** с графом: в графе свободный текст без tool call
уходит в `handle_error`, а в `LLMPlanner` — трактуется как уточнение. Это
расхождение зафиксировано и не меняется.

## Последствия

**Плюсы.**

- OpenAI/DeepSeek/Ollama получают нативный tool calling; JSON-адаптер остаётся
  fallback и не ломает несовместимые модели.
- Каждый шаг графа виден в `audit.jsonl`; вопрос и секреты не утекают.
- `LLMPlanner` и граф используют один и тот же `CompletionResult`.

**Минусы и риски.**

- `supports_tools=True` меняет поведение `LLMPlanner`: при свободном тексте
  без tool call он трактует ответ как уточнение. Для графа это не влияет —
  там свободный текст уходит в `handle_error`.
- Fallback по тексту ошибки эвристический; при изменении формулировок
  провайдера тест на 400/«tools unsupported» нужно обновлять.
- Аудит пишется в `.mcp_state/audit.jsonl` (вне git); в тестах путь
  изолируется через `ONEC_MCP_AUDIT_FILE`.

**Вне области:** кассеты/live/judge (этап 4), ruff/mypy/Docker/0.8.0
(этап 5), удаление legacy `GigaChatClient` (отдельно).

## Верификация

- `pytest -q` → **754 passed** (728 после этапа 3 + 26 новых в 3.1:
  22 негативных `LLMClient` + 4 аудита).
- `tests/test_llm_client_tools.py` — сужение fallback: 400 invalid model /
  500 unknown function / сеть / 422 схема → `LLMError` без fallback;
  400 с маркером неподдержки tools → JSON-fallback; падение fallback →
  `LLMError`; несколько tool_calls / битый JSON / content+tool_calls /
  пустой ответ.
- `tests/test_agent_audit.py` — `step`/`duration_ms` в каждом событии,
  отдельное событие `fallback`, `graph_end` со `status`/`fallback`/`error_type`,
  категории `error_type`, отсутствие вопроса и credentials, сбой аудита не
  ломает ответ.
- `tests/test_gigachat.py` — `LLMClient` отправляет tools и разбирает
  `tool_calls`; fallback на JSON при «tools unsupported».
- `tests/test_agent_graph*.py`, `test_agent_nodes.py` — без регрессий.

## Альтернативы

1. **Оставить JSON только.** Отклонено: ТЗ 6.4 §1 требует нативный tool
   calling; GigaChat уже на нём.
2. **Падать при отсутствии tools у провайдера.** Отклонено: Ollama/локальные
   модели и часть эндпоинтов остались бы неработоспособными.
3. **Писать в аудит весь state.** Отклонено: утечка вопроса/ответа/секретов.
4. **Считать лимит по аудиту.** Отклонено: аудит не должен влиять на решение.
