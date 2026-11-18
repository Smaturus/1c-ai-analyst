# ADR 0014: GigaChat в LangGraph-агенте через `langchain-gigachat`

- **Статус:** принято
- **Дата:** 2026-11-18
- **Этап:** 2.5 (перевод провайдера GigaChat на официальную библиотеку)
- **Связанные решения:** [ADR 0012](0012-gigachat-provider.md) (GigaChat как провайдер), [ADR 0013](0013-langgraph-hybrid-contract.md) (граф, контракт DSL)
- **Связанные документы:** ТЗ «Гибридная архитектура LangGraph» ревизии 6.4, §1 (провайдерный слой), `docs/REPORT_LANGGRAPH_STAGE25.md`
- **Затрагивает:** `src/onec_analyst/agent/llm.py` (новый), `src/onec_analyst/agent/graph.py`, `pyproject.toml`, `requirements.txt`

## Контекст

К этапу 2 GigaChat в графе работал через самописный
`onec_analyst.llm_agent.GigaChatClient`: OAuth `client_credentials`, TLS с
корнем НУЦ Минцифры, ручной разбор `chat/completions` и `tool_calls`.
Клиент выполнял контракт, но требовал собственного сопровождения:
обновления API, повторов при 401, разбора ошибок, поддержки TLS-настроек.
При этом `langchain-core` уже стоит как зависимость `langgraph`, а
официальная `langchain-gigachat` совместима с `langchain-core>=1,<2`
(проверено: 0.5.1 + `gigachat` 0.2.4 + `langchain-core` 1.8.2 +
`langgraph` 1.2.22).

Графу нужен только маленький интерфейс клиента:

```text
client.supports_tools: bool
client.complete(messages: list[dict], tools: list[dict] | None) -> CompletionResult
```

## Решения

### D1. Адаптер — отдельный модуль `agent/llm.py`

**Решение.** Официальная библиотека подключается адаптером
`LangChainGigaChatClient`, а не напрямую в узлах. Узлы, промпты, `tools.py`,
`state.py`, `pipeline.py`, `dispatcher.py` не меняются: адаптер реализует уже
существующий интерфейс клиента и возвращает те же
`CompletionResult`/`ToolCall` из `llm_agent`.

**Обоснование.** Контракт графа остаётся прежним, а замена библиотеки
локализована в одном файле. Это позволяет позже добавить нативные tools для
OpenAI/DeepSeek/Ollama, не трогая узлы.

### D2. Маппинг `LLMSettings` → `GigaChat(...)`

| `LLMSettings` | `GigaChat` | Примечание |
|---|---|---|
| `api_key` | `credentials` | authorization key, в логи не попадает |
| `scope` | `scope` | `GIGACHAT_API_PERS/CORP/B2B` |
| `model` | `model` | дефолт проекта `GigaChat-2-Pro` |
| `base_url` | `base_url` | пустая строка → `None` (дефолт SDK) |
| `verify_tls` | `verify_ssl_certs` | `False` — только dev-флаг |
| `ca_bundle` | `ca_bundle_file` | пустая строка → `None`; корень НУЦ |
| `temperature` | `temperature` | `0.0` по умолчанию |
| `timeout` | `timeout` | таймаут запроса |

`auth_url` в адаптер не передаётся: SDK использует официальный OAuth-эндпоинт
Сбера. Переменная `GIGACHAT_AUTH_URL` остаётся для legacy-клиента
(health-check), где она исторически применялась.

### D3. Совместимость JSON-схем инструментов с GigaChat

**Проблема (найдена spike'ом на живом API).** Pydantic 2 генерирует для
`Optional[date]` схему `anyOf: [{type: string}, {type: null}]`. GigaChat
отвечает `422 Type properties.start_date.type is wrong`: у свойства нет
верхнеуровневого `type`, а `anyOf` провайдер не поддерживает. Версия
`langchain-gigachat` 0.5.1 для готовых dict-инструментов пропускает схему
как есть (`convert_to_gigachat_tool` не вызывает `gigachat_fix_schema`),
поэтому ошибка доходит до API.

**Решение.** `normalize_tool_schemas()` в адаптере рекурсивно сводит
`anyOf` с единственным не-`null` вариантом к этому варианту и проставляет
`type: object` объектным узлам. `anyOf` из двух содержательных типов
(настоящий `Union`) даёт `LLMError`: GigaChat такие аргументы не поддерживает.
Схемы по-прежнему строятся из Pydantic-моделей
(`agent.tools.build_agent_tools()`), меняется только транспортное
представление, а не контракт 7 интентов.

Это осознанное отклонение от буквального «передать `build_agent_tools()` в
`bind_tools`»: без нормализации живой GigaChat отвечает 422 (проверено).
Тест фиксирует и совпадение имён/описаний инструментов с
`build_agent_tools()`, и отсутствие `anyOf` в отправленных схемах.

### D4. Граф использует адаптер; legacy-клиент остаётся для health-check

**Решение.** `graph.run_agent_graph` создаёт клиента через
`agent.llm.make_agent_client(settings)`:

* `provider == "gigachat"` → `LangChainGigaChatClient`;
* остальные провайдеры → `onec_analyst.llm_agent.make_client` (без изменений).

Самописный `GigaChatClient` **не удаляется** и помечен в docstring как
legacy для health-check/списка моделей. `check_provider`, `chat_models`,
`--check` и UI-список моделей продолжают работать через него; `test_gigachat.py`
не менялся.

### D5. Ленивый импорт и единый тип ошибок

`langchain_gigachat` импортируется только при первом вызове GigaChat, поэтому
импорт `agent.llm` и офлайн-тесты не тянут SDK. Любое исключение SDK
(сеть, TLS, HTTP, аутентификация) заворачивается в `LLMError` — узлы графа
уже обрабатывают именно его (`except LLMError`), и падение провайдера
превращается в `handle_error`, а не в «похожий ответ».

### D6. Явный выбор модели: единственный источник — `settings.model`

**Решение.**

* **Единственный источник модели — `settings.model`.** Адаптер передаёт
  `model=settings.model` в `GigaChat(...)`; хардкод (`"GigaChat"`,
  `GigaChat()` без модели), автоперебор моделей и подмена выбранной модели
  «на похожую» запрещены.
* **Никакого pre-check по `/models`.** Список моделей не блокирует вызов:
  если выбранной модели нет в списке или она недоступна ключу, это выясняется
  при вызове, а не подменой настройки.
* **Ошибка модели/конструктора/вызова → `LLMError`.** В `complete()` в `try`
  входит весь путь (`_get_model()`, `bind_tools`, `invoke`). Текст:
  `GigaChat (<settings.model>) недоступен: <Type>: <краткий текст>`.
  `Credentials` вырезаются из текста (`***`), имя модели берётся только из
  настроек, не из ответа.
* **Ошибка → `handle_error` → `fallback` (офлайн-правила) или `error`.** Модель
  в настройках/состоянии при этом не меняется.
* **Список моделей для UI — по-прежнему legacy `GigaChatClient.chat_models()` /
  `GET /models`.** Если выбранная модель отсутствует в списке, UI показывает её
  как выбранную и не подменяет (поведение `app.py` сохранено).

**Обоснование.** Явный выбор модели — часть пользовательского контракта: смена
модели влияет на стоимость и качество. Молчаливый fallback на другую модель
скрыл бы ошибку конфигурации и сделал бы ответы невоспроизводимыми. Ошибка
конструктора, выброшенная мимо `complete()`, ломала бы `handle_error`, поэтому
`try` охватывает и создание клиента.

**Проверка.** `tests/test_agent_llm_gigachat.py`: три разные `settings.model`
доходят до `GigaChat(model=...)`; ошибки конструктора/`invoke`/`bind_tools` →
`LLMError` с именем модели; credentials не утекают; `/models` не вызывается.
`tests/test_agent_graph_gigachat.py`: ошибка модели → `handle_error` →
`fallback`/`error`, модель в клиенте не подменена.

## Последствия

**Плюсы.**

* OAuth, TLS, повторы и разбор ответа GigaChat сопровождает официальная
  библиотека; проект не поддерживает свой HTTP-клиент в агентском пути.
* Меньше кода на критическом пути; `AIMessage.tool_calls` —
  стандартная структура LangChain (`name`, `args`, `id`), а не ручной JSON.
* Совместимость с `langchain-core` 1.x, на которой уже стоит `langgraph`.

**Минусы и риски.**

* `langchain-gigachat` тянет `gigachat>=0.2.1,<0.3` — новая транзитивная
  зависимость и её обновления нужно отслеживать.
* Схемы инструментов проходят нормализацию `anyOf`; при изменении
  Pydantic-моделей тест на отсутствие `anyOf` поймает регрессию.
* `langchain-gigachat` пока не даёт streaming/structured output в объёме,
  нужном этапу 3; это отдельная задача.

**Вне области решения:** нативные tools для OpenAI/DeepSeek/Ollama,
`audit.jsonl`, кассеты этапа 3, коммиты/push. `app_local.py` не публикуется.

## Альтернативы

1. **Оставить самописный клиент.** Отклонено: сопровождение OAuth/TLS/разбора
   ошибок дублирует официальную библиотеку и расходится с ТЗ 6.4 §1.
2. **Вызывать `ChatGigaChat` прямо в узлах.** Отклонено: узлы знали бы про
   LangChain-сообщения и `AIMessage`, контракт графа размылся бы.
3. **OpenAI-совместимый эндпоинт GigaChat.** Отклонено: нет паритета по
   tool calling и OAuth-настройкам, уже покрытым `langchain-gigachat`.

## Верификация

* `pytest -q` → **710 passed** (683 baseline + 27 новых), 20.43 с; после
  дополнения 2.5a — **722 passed** (ещё +12: явная модель и ошибки
  конструктора/вызова).
* D6: три разные `settings.model` доходят до `GigaChat(model=...)`; ошибки
  конструктора/`invoke`/`bind_tools` → `LLMError` с именем модели и без
  credentials; граф уходит в `handle_error`/`fallback`, модель не подменяется.
* `tests/test_agent_llm_gigachat.py` — маппинг конструктора, `bind_tools(...,
  tool_choice="auto")`, `tool_calls`, `content`, `invalid_tool_calls`,
  пустой ответ, ленивый импорт, ошибки SDK.
* `tests/test_agent_graph_gigachat.py` — граф с fake GigaChat: 1 call → `ok`,
  0/2 → `handle_error`, invalid → `fix_dsl` → `ok`, `clarify`, `refuse`,
  другие провайдеры не затронуты.
* Живой spike (без коммита): `GigaChat(...)` + `bind_tools(build_agent_tools(),
  tool_choice="auto").invoke(...)` → `AIMessage.tool_calls=[{name: "total",
  args: {...}, id: ..., type: "tool_call"}]`, `invalid_tool_calls=[]`,
  `content=""`; TLS с корнем НУЦ проходит, без корня —
  `CERTIFICATE_VERIFY_FAILED`.
* `python -m onec_analyst.llm_agent --check` → `OK gigachat: GigaChat-2-Pro`
  (legacy-клиент и список моделей не сломаны).
