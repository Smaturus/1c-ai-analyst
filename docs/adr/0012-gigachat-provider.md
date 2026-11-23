# ADR 0012: GigaChat как провайдер планировщика (tool calling)

- **Статус:** предложено
- **Дата:** 2026-11-17
- **Контекст:** шаг интеграции GigaChat (Сбер) для Сценария 1 «интеллектуальное
  управление запросами и уточнениями». Требуется понять формулировки и
  уточнять неоднозначные вопросы. GigaChat **не OpenAI-совместим**: авторизация —
  двухшаговый OAuth `client_credentials` (обмен authorization key на access token,
  заголовок `RqUID`, `scope`), TLS через самоподписанные сертификаты с доверием к
  корню НУЦ Минцифры, а `response_format={"type":"json_object"}` отсутствует —
  вместо него есть tool calling (function calling).

## Решение

1. **Новый провайдер `gigachat`** в `LLM_PROVIDER` (рядом с `none`/`openai`/
   `deepseek`/`ollama`). Модель по умолчанию — `GigaChat-2-Pro`, базовый URL —
   `https://api.giga.chat/v1`, эндпоинт токена —
   `https://ngw.devices.sberbank.ru:9443/api/v2/oauth` (переопределяется env).
   Префикс `/v1` без `/api`: хост API — `api.giga.chat`, и путь берётся от
   документации метода (`GET https://api.giga.chat/v1/models`), а не от старого
   хоста `gigachat.devices.sberbank.ru/api/v1`. С `/api/v1` запросы получают
   `403 Forbidden` от nginx, хотя OAuth уже прошёл.
2. **OAuth `client_credentials`:** `GigaChatClient` обменивает authorization key
   (`Authorization: Basic base64(client_id:client_secret)` + `RqUID: <uuid>` +
   `scope` в теле) на access token (живёт ~30 минут); токен кэшируется с запасом,
   при `401` на chat-запросе — сброс кэша и повторное получение.
3. **TLS:** доверие к корню НУЦ Минцифры через `LLM_TLS_CA_BUNDLE` (путь к
   `.crt`); по умолчанию проверка включена. `LLM_TLS_VERIFY=0` — явный dev-флаг
   отключения проверки, не используется молча.
4. **Tool calling вместо `response_format`:** `build_tools()` строит по одной
   функции на интент из Pydantic-моделей параметров (`_PARAMS_MODELS`). Модель
   возвращает **один** вызов функции (ограничение GigaChat: один вызов за запрос),
   а её аргументы валидируются **тем же** `_DSL_ADAPTER` (discriminated union
   `IntentDSL`), что и JSON-DSL. Свободный текст без вызова функции трактуется как
   уточняющий вопрос (`clarification`).
5. **Клиент и планировщик:** новый `GigaChatClient` (OAuth + TLS + разбор
   `tool_calls`) рядом с `LLMClient`; фабрика `make_client()`. `LLMPlanner`
   вызывает нормированный `complete()` → `CompletionResult` (`content` | `tool_calls`)
   с обратной совместимостью к fake-клиентам с `chat()`. Одна попытка «починки»
   невалидных аргументов сохранена.
6. **Инварианты не меняются:** модель выбирает только интент и параметры, числа
   считает pandas (`dispatch_intent`); `guard_decision`, Pydantic `extra="forbid"`,
   офлайн-фолбэк `RuleBasedPlanner` и запись каждого вызова в `audit.jsonl` —
   без изменений.
7. **Секреты только в env:** `LLM_API_KEY` (authorization key), `GIGACHAT_SCOPE`
   (по умолчанию `GIGACHAT_API_PERS`), `GIGACHAT_AUTH_URL`, `LLM_TLS_CA_BUNDLE`.
   В репозиторий и логи значения не попадают.
8. **Несколько провайдеров одновременно:** провайдерные ключи
   `LLM_API_KEY_<PROVIDER>` / `LLM_MODEL_<PROVIDER>` / `LLM_BASE_URL_<PROVIDER>`
   и `provider_catalog()`. В сайдбаре — переключатель **«Провайдер LLM»** со
   всеми поддерживаемыми провайдерами; ненастроенные помечены «— не настроен»
   (при выборе — предупреждение и офлайн-правила). По умолчанию — настроенный
   провайдер из `LLM_PROVIDER`. DeepSeek и GigaChat доступны в одном приложении.
9. **Модель GigaChat выбирается в UI, но живёт в сессии:** `chat_models()` отдаёт
   только модели генерации из `GET /v1/models` (`type == "chat"`, эмбеддинги
   отброшены), а `choose_gigachat_model()` рисует под переключателем провайдера
   список «Модель GigaChat». Выбор меняет модель через `dataclasses.replace` и
   **не** пишется в `.env`: приложение не редактирует файл с секретами, поэтому
   после перезапуска действует `LLM_MODEL_GIGACHAT`. Если список недоступен,
   селектора нет; модель из `.env`, отсутствующая в списке API, показывается
   первой и не подменяется автоматически.

## Обоснование

1. **Единый контракт:** tool calling не вводит второго контракта — аргументы
   функции сворачиваются в `{"intent": ..., "params": ...}` и проходят тот же
   Pydantic-валидатор, что и текстовый JSON-DSL. «Фантазии» модели не доходят
   до pandas.
2. **Жёсткая типизация:** JSON-схема инструментов генерируется из моделей
   параметров, поэтому рассинхронизация с `INTENT_REGISTRY` исключена
   (`check_schema_sync` по-прежнему сверяет интенты/параметры).
3. **Ноль новых зависимостей:** OAuth/TLS/tool calling реализованы на
   `urllib.request` + `ssl`, как и существующий `LLMClient` (см. [ADR 0006](0006-llm-providers.md)).
4. **Безопасная деградация:** при недоступности GigaChat `LLMPlanner` возвращает
   `ok=False`, а приложение показывает дружелюбную ошибку со списком интентов;
   без ключа — `RuleBasedPlanner` (офлайн-режим не сломан).
5. **ИБ:** передача данных в облако Сбера — отдельное решение (маскирование/
   агрегатный режим/частный контур); это изменение касается только тестовой
   витрины с вымышленными контрагентами.

## Рассмотренные альтернативы

- **Свободный текст (JSON-DSL) без инструментов** — отклонено как основной путь:
  GigaChat не поддерживает `response_format=json_object`, строгий JSON даёт менее
  гарантированный выход, чем типизированные аргументы функции. Текстовый путь
  сохранён для OpenAI-совместимых провайдеров и для классификатора уточнений.
- **Structured output через `response_format=json_schema`** (эмуляция function call)
  — отклонено: по факту это тот же механизм, но менее явный; явный `tools` понятнее
  и покрывается официальной документацией.
- **SDK `gigachat` / `langchain-gigachat`** — отклонено: лишняя зависимость;
  нужен лишь OAuth + chat/completions + tool calling, которые делаются стандартной
  библиотекой (политика ADR 0006).
- **Один инструмент `dispatch` со схемой-union** — допустимо, но семь именованных
  функций читабельнее и позволяют модели явно выбрать интент.

## Последствия

- `src/onec_analyst/llm_agent.py`: провайдер `gigachat`, поля `LLMSettings`
  (`scope`/`auth_url`/`ca_bundle`/`verify_tls`), `build_tools()`, `CompletionResult`,
  `ToolCall`, `GigaChatClient`, `make_client()`, `complete()` в `LLMClient`,
  `LLMPlanner` с поддержкой tool calling.
- `src/onec_analyst/clarification.py`: `classify_with_llm` использует `make_client()`
  (работает и с GigaChat).
- `app.py`: переключатель «Провайдер LLM» в сайдбаре (`available_providers()` +
  `choose_llm_provider`), несколько провайдеров в одном запуске; селектор модели
  GigaChat (`choose_gigachat_model`) — выбор на сессию, без записи в `.env`.
  Те же правки — в `app_local.py` (правило двух версий UI).
- Тесты: `tests/test_gigachat.py` (fake-транспорт, без сети/секретов) — OAuth,
  кэш токена, повтор при 401, разбор `tool_calls`, уточнение свободным текстом,
  починка аргументов, graceful degradation.
- Модель/модель-ряд и `scope` — предмет договора с заказчиком; перед боевой
  базой — решение ИБ по передаче данных (маскирование/контур).
- Связанные решения: [ADR 0004](0004-llm-json-dsl.md), [ADR 0006](0006-llm-providers.md),
  [ADR 0007](0007-graceful-degradation.md).
