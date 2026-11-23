# Отчёт этапов 2.5 и 2.5a: GigaChat через `langchain-gigachat`

- **Дата:** 2026-11-18
- **Статус:** код и тесты готовы; `pytest -q` → **722 passed**. Коммит/push не выполнялись (по договорённости).
- **Baseline:** `2d8f32f` (`main`), рабочее дерево с незакоммиченным этапом 2.
- **До изменений (2.5):** `pytest -q` → **683 passed** (20.48 с), совпадает с `REPORT_LANGGRAPH_STAGE2.md`.
- **После 2.5:** `pytest -q` → **710 passed** (20.43 с): 683 + 27 новых.
- **После 2.5a:** `pytest -q` → **722 passed** (19.88 с): 710 + 12 новых (§9).
- **ADR:** [0014](adr/0014-gigachat-langchain.md).

## 1. Цель и границы

Граф LangGraph-агента должен получать GigaChat из официальной
`langchain-gigachat`, а не из самописного HTTP-клиента, **не меняя** граф,
промпты, статусы и контракт:

```text
client.supports_tools: bool
client.complete(messages, tools) -> CompletionResult
client.chat(messages) -> str
```

Не менялись: `agent/nodes.py`, `agent/prompts.py`, `agent/tools.py`,
`agent/state.py`, `pipeline.py`, `dispatcher.py`, `analytics`, Pydantic-модели,
GoldSet. `app_local.py` не публиковался. Нативные tools для
OpenAI/DeepSeek/Ollama, `audit.jsonl` и кассеты — вне этапа.

## 2. Версии пакетов (факт из окружения)

| Пакет | Версия | Примечание |
|---|---|---|
| Python | 3.14.6 | `.venv` проекта |
| `langchain-gigachat` | **0.5.1** | установлена в этапе 2.5 |
| `gigachat` | **0.2.4** | транзитивная зависимость (`>=0.2.1,<0.3`) |
| `langchain-core` | **1.8.2** | была до этапа (зависимость `langgraph`) |
| `langgraph` | **1.2.22** | без изменений |
| `pydantic` | **2.14.1** | без изменений |

`pyproject.toml` и `requirements.txt`: добавлено `langchain-gigachat>=0.5,<0.6`.

## 3. Spike на живом GigaChat (без коммита)

Скрипты spike удалены после прогона; ключи, токены и сертификаты не
логировались.

**Конструктор (фактические аргументы):**

```python
GigaChat(
    credentials=settings.api_key,          # authorization key
    scope=settings.scope,                  # GIGACHAT_API_PERS/CORP/B2B
    model=settings.model,                  # GigaChat-2-Pro
    base_url=settings.base_url or None,    # https://api.giga.chat/v1
    verify_ssl_certs=settings.verify_tls,
    ca_bundle_file=settings.ca_bundle or None,
    temperature=settings.temperature,
    timeout=settings.timeout,
)
```

**TLS.**

- с `ca_bundle_file=russian_trusted_root_ca_pem.crt` — OAuth и запрос проходят;
- без корня НУЦ — `ConnectError: [SSL: CERTIFICATE_VERIFY_FAILED]
  self-signed certificate in certificate chain`.

**Tool calling.** Первый spike с `build_agent_tools()` **как есть** дал
`422 Type properties.start_date.type is wrong`: Pydantic 2 генерирует
`anyOf: [{type: string}, {type: null}]`, а GigaChat требует верхнеуровневый
`type` и не поддерживает `anyOf`. После нормализации схем (`anyOf` с
единственным не-`null` вариантом → этот вариант) живой вызов вернул:

```text
AIMessage(
  content="",
  tool_calls=[{
      "name": "total",
      "args": {"start_date": "2024-01-01", "end_date": "2024-12-31"},
      "id": "79af03f1-…",
      "type": "tool_call",
  }],
  invalid_tool_calls=[],
  additional_kwargs={"function_call": {...}, "functions_state_id": "..."},
  response_metadata={"finish_reason": "function_call", "model_name": "GigaChat-2-Pro:2.0.30.01", ...},
)
```

Именно этот формат (`name`, `args`, `id`) адаптер переводит в
`ToolCall(name, arguments)`; `content` при tool call — пустая строка.

## 4. Что сделано

| Файл | Изменение |
|---|---|
| `src/onec_analyst/agent/llm.py` | **новый**: `LangChainGigaChatClient`, `make_agent_client`, `normalize_tool_schemas` |
| `src/onec_analyst/agent/graph.py` | `llm_agent.make_client(current)` → `make_agent_client(current)`; `_OnCallClient`, guard, маршрутизация не тронуты |
| `src/onec_analyst/llm_agent.py` | docstring `GigaChatClient` помечен «LEGACY для health-check/списка моделей»; код не менялся |
| `pyproject.toml`, `requirements.txt` | `langchain-gigachat>=0.5,<0.6` |
| `tests/test_agent_llm_gigachat.py` | **новый**, 27 тестов адаптера (18 в 2.5 + 9 в 2.5a) |
| `tests/test_agent_graph_gigachat.py` | **новый**, 12 тестов графа с fake GigaChat (9 в 2.5 + 3 в 2.5a) |
| `docs/adr/0014-gigachat-langchain.md` | **новый** ADR |
| `docs/ARCHITECTURE.md` | провайдерный слой: `llm_agent.py` (legacy) + `agent/llm.py` |
| `README.md` | одна строка: GigaChat в агенте — через `langchain-gigachat` |
| `docs/INDEX.md` | строка ADR 0014, строка отчёта 2.5, счётчик 710 |

### 4.1. Адаптер `agent/llm.py`

- `supports_tools = True`; `complete(messages, tools=None)`; `chat(messages)`.
- Ленивый импорт `langchain_gigachat` и `langchain_core.messages`: импорт
  модуля и офлайн-тесты SDK не тянут (проверено подпроцессом).
- Сообщения: `system → SystemMessage`, `user/human → HumanMessage`,
  `assistant/ai → AIMessage`; неизвестная роль — `LLMError`.
- `tools` → `normalize_tool_schemas(tools)` → `bind_tools(..., tool_choice="auto")`;
  без `tools` — обычный `invoke`.
- Ответ: `AIMessage.tool_calls` → `ToolCall`; `content` (в т.ч. список блоков)
  → `str`; `invalid_tool_calls`, не-объект `args`, tool call без имени → `LLMError`.
- Любое исключение SDK (сеть/TLS/HTTP) → `LLMError`: узлы графа ловят именно
  его, поэтому сбой провайдера даёт `handle_error`, а не падение графа.
- Ошибка конфигурации: `provider != "gigachat"` → `ValueError` в конструкторе.

### 4.2. Фабрика

```python
def make_agent_client(settings: LLMSettings):
    if settings.provider == "gigachat":
        return LangChainGigaChatClient(settings)
    return llm_agent.make_client(settings)
```

`graph.run_agent_graph` использует её только когда `client` не передан.
При `provider != "gigachat"` поведение прежнее (`LLMClient`), это покрыто
тестом.

### 4.3. Старый `GigaChatClient`

Не удалён и не изменён функционально — legacy для `check_provider`,
`chat_models`, `--check` и списка моделей GigaChat в сайдбаре
(`app.py` вызывает `make_client`/`chat_models`). `tests/test_gigachat.py`
не менялся.

Живая проверка после изменений:

```text
OK      deepseek: deepseek-chat — доступно: deepseek-flash, deepseek-v4-pro
OK      gigachat: GigaChat-2-Pro — доступно: GigaChat-2, GigaChat-2-Max, GigaChat-2-Pro, …,
        Embeddings, EmbeddingsGigaR, GigaEmbeddings-3B-2025-09
```

## 5. Тесты

### Новые (27)

`tests/test_agent_llm_gigachat.py` (18):

1. `langchain_gigachat` не импортируется вместе с `agent.llm`;
2. маппинг конструктора (`credentials/scope/model/base_url/TLS/ca_bundle/temperature/timeout`);
3. пустые `base_url`/`ca_bundle` → `None`;
4. `provider != gigachat` → `ValueError`;
5. `make_agent_client`: gigachat → адаптер, deepseek → прежний `LLMClient`;
6. `bind_tools` получил схемы `build_agent_tools()` и `tool_choice="auto"`,
   у всех свойств есть `type` и нет `anyOf`;
7. `normalize_tool_schemas` сводит `anyOf`+`null`, не мутируя исходник;
8. настоящий `Union` → `LLMError`;
9. `AIMessage.tool_calls` → `CompletionResult(ToolCall(...))`;
10. `content` без tools → `CompletionResult(content=...)`;
11. `chat()` → строка;
12. `content`-список → строка;
13. `invalid_tool_calls` → `LLMError`;
14. `args`-не-объект → `LLMError`;
15. пустой ответ → пустой `CompletionResult`;
16. неизвестная роль → `LLMError`; исключение SDK → `LLMError`;
    legacy `make_client` по-прежнему отдаёт `GigaChatClient`.

`tests/test_agent_graph_gigachat.py` (9):

- один tool call → `ok`, клиент графа — `LangChainGigaChatClient`;
- 0 tool calls → `handle_error`;
- 2 tool calls → `handle_error`;
- невалидные аргументы → `fix_dsl` (текстовый JSON) → `ok`, ровно 2 вызова;
- повторная ошибка после починки → `handle_error`;
- `invalid_tool_calls` от SDK → `handle_error`;
- `clarify` → статус `clarify`, без `validate_dsl`;
- `refuse` → статус `refuse`, без `validate_dsl`;
- `provider=deepseek` идёт через `llm_agent.make_client`, не через адаптер.

### Команды и результат

```text
.\.venv\Scripts\python.exe -m pytest tests\test_agent_llm_gigachat.py tests\test_agent_graph_gigachat.py -q
# 27 passed in 1.01s

.\.venv\Scripts\python.exe -m pytest -q
# 710 passed in 20.43s
```

`py_compile` для `agent/llm.py`, `agent/graph.py`, `llm_agent.py` — без ошибок.
AppTest-тесты UI (в общем прогоне) зелёные — путь `dispatcher → граф` не сломан.

## 6. Отклонение от буквального ТЗ (осознанное)

ТЗ этапа 2.5 предполагало `bind_tools(build_agent_tools(), tool_choice="auto")`
без преобразований. Живой API показал `422` на `anyOf`, который генерирует
Pydantic для `Optional`. Поэтому адаптер нормализует **транспортное**
представление схем:

- `anyOf: [X, null]` → `X` (с сохранением `format`, `description`, `default`);
- объектным узлам без `type` проставляется `type: object`;
- настоящий `Union` (два содержательных типа) → `LLMError`.

Контракт 7 интентов, `build_agent_tools()` и Pydantic-модели не менялись.
Тест проверяет совпадение имён/набора инструментов с `build_agent_tools()` и
отсутствие `anyOf` в отправленных схемах. Причина, последствия и альтернативы
зафиксированы в [ADR 0014](adr/0014-gigachat-langchain.md) (D3).

## 7. Риски и откат

**Остаточные риски.**

- `langchain-gigachat` тянет `gigachat<0.3`: обновления библиотеки нужно
  проверять на живом API (spike-сценарий из §3).
- Нормализация схем — точка хрупкости при изменении Pydantic-моделей; тест
  «нет `anyOf`» поймает регрессию.
- Legacy `GigaChatClient` остаётся вторым HTTP-клиентом GigaChat; его
  удаление/перевод health-check на SDK — отдельная задача (этап 3+).
- В `README.md`/`RUNBOOK.md`/`UI_ACCEPTANCE.md` остались исторические
  счётчики 393/319 (этап 2 их тоже не обновлял); актуальный источник —
  вывод `pytest` и этот отчёт.

**Откат.** Изменения не закоммичены. Достаточно вернуть `agent/graph.py`
на `llm_agent.make_client`, удалить `agent/llm.py` и два новых теста,
убрать строку зависимости и документные правки; `llm_agent.py` и
`GigaChatClient` функционально не менялись, поэтому откат не затрагивает
этап 2.

## 8. Что осталось на этап 3

- Нативные tools для OpenAI/DeepSeek/Ollama (сейчас — JSON-адаптер в
  `tools.parse_completion`), единый провайдерный слой для всех.
- `audit.jsonl` и кассеты ответов (CI-replay без сети).
- Перевод health-check/списка моделей на `langchain-gigachat` и удаление
  legacy `GigaChatClient` (после стабилизации).
- Streaming/structured output GigaChat, если понадобится UI.
- Актуализация счётчиков тестов в `README.md`/`RUNBOOK.md`/`UI_ACCEPTANCE.md`
  и `ARCHITECTURE-SOLUTION.md`.
- Коммит/push этапов 2, 2.5 и 2.5a — отдельной командой.

## 9. Дополнение 2.5a: явный выбор модели и ошибки конструктора

**Проблема.** В `LangChainGigaChatClient.complete()` вызов `self._get_model()`
стоял **до** `try`. Ошибка конструктора `GigaChat(...)` (неверная модель,
credentials) вылетала сырым исключением SDK мимо `LLMError`, поэтому граф не
уходил в `handle_error`. Ошибки `bind_tools` тоже были вне `try`.

**Исправление.** В `try` теперь входит весь путь: `_get_model()`,
`normalize_tool_schemas` + `bind_tools`, `_to_langchain_messages` + `invoke`.
`LLMError` пробрасывается как есть; любое другое исключение заворачивается в
`LLMError("GigaChat (<settings.model>) недоступен: <Type>: <краткий текст>")`.
Имя модели берётся только из `settings.model`; `credentials` вырезаются из
текста (`self._short_error` → `***`). Кэш модели живёт внутри одного клиента:
каждый `make_agent_client(settings)` создаёт `GigaChat(model=settings.model)`
заново, чужая модель не переиспользуется.

**Тесты (+12).** `tests/test_agent_llm_gigachat.py`:

1. `settings.model` параметризован: `GigaChat-2-Pro`, `GigaChat-Max`,
   `Custom-Model` — ровно это значение уходит в `GigaChat(model=...)`;
2. два клиента с разными моделями создают два разных `GigaChat`; повторный
   вызов того же клиента второй клиент не создаёт;
3. ошибка конструктора (`RuntimeError("model not found")`) → `LLMError` с
   именем модели;
4. credentials в тексте ошибки конструктора заменены на `***`;
5. ошибка `invoke` (`404 model not found`) → `LLMError` с именем модели;
6. ошибка `bind_tools` → `LLMError` с именем модели;
7. адаптер не обращается к `/models`: вызывается только `invoke`, модель не
   подменяется по списку.

`tests/test_agent_graph_gigachat.py`:

8. ошибка конструктора → `handle_error` → `fallback`/`error`, модель в клиенте
   остаётся выбранной (`Custom-Model`), экземпляр `GigaChat` не создан;
9. текст ошибки содержит имя модели и тип, не содержит credentials;
10. ошибка `invoke` → `handle_error`, `GigaChat(model="GigaChat-Max")` не
    подменяется.

**Результат.** `pytest -q` → **722 passed** (19.88 с). Живая проверка
несуществующей модели (`GigaChat-No-Such-Model`) даёт
`LLMError: GigaChat (GigaChat-No-Such-Model) недоступен: NotFoundError: 404 …
No such model` — имя модели есть, credentials нет. `/models`, UI-список
моделей, `check_provider`, `chat_models`, `test_gigachat.py` не менялись.
Документы: ADR 0014 D6, `ARCHITECTURE.md` (ТЗ 6.4 §1), `README.md`.

## 10. Трассировка и соответствие

| Требование / ADR | Изменение | Проверка |
|---|---|---|
| ADR 0012, 0013 (контракт не меняется) | адаптер вместо правок узлов | 722 passed, AppTest |
| ADR 0014 D1 (интерфейс клиента) | `LangChainGigaChatClient` | `test_agent_llm_gigachat.py` |
| ADR 0014 D2 (маппинг настроек) | `_constructor_kwargs` | `test_constructor_maps_llm_settings` |
| ADR 0014 D3 (схемы GigaChat) | `normalize_tool_schemas` | `test_bind_tools…`, `test_normalize…`, живой spike |
| ADR 0014 D4 (legacy) | docstring + `make_client` не тронут | `test_make_client_still_returns_legacy…`, `--check` |
| ADR 0014 D6 (явная модель) | `try` вокруг `_get_model`/`bind_tools`/`invoke`, `model=settings.model` | `test_settings_model_is_the_only_model_source`, `test_constructor_error…`, `test_invoke_model_error…` |
| ТЗ 6.4 §1 (провайдерный слой) | `agent/llm.py`, `ARCHITECTURE.md` | этот отчёт, ADR |

| Раздел отчёта | Стандарт |
|---|---|
| Baseline, версии, трассировка | IEEE 828, ISO 10007 |
| Цель, границы, изменения | ISO/IEC/IEEE 12207 |
| Классификация и документирование | ISO/IEC/IEEE 15289 |
| Верификация (pytest, spike, smoke) | ISO/IEC/IEEE 29119-3 |
| Риски, секреты, остаточные риски | ISO/IEC 27002 |
| Качество (совместимость, сопровождаемость) | ISO/IEC 25010 |
