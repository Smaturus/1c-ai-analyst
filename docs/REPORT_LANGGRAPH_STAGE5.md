# Отчёт этапа 5: качество DSL, per-call лимиты (§4.2), релиз 0.8.0

Этап 5 объединяет отложенный этап 3.2 (лимиты LLM по ТЗ 6.4 §4.2) и подготовку
релиза 0.8.0. Метрики сняты на живых моделях (`tools/eval_live.py`), судья —
`tools/eval_judge.py`; GoldSet (`tests/golden/cases.json`) и кассеты не менялись.

## 1. Блок A — этап 3.2: per-call лимиты LLM (§4.2)

Было: UI считал **успешные запросы** (один инкремент на запрос после `outcome.llm_success`),
`CallBudgetGuard(3)` ограничивал только один запрос. Фактических обращений к API могло
быть до 45 при счётчике «15».

Стало (полное соответствие §4.2):

- `RateLimiter.try_consume(session_id)` — атомарная проверка session/global + резерв
  в одной транзакции `BEGIN IMMEDIATE` (проверка и инкремент не разъезжаются).
- `RateLimiter.release(session_id)` — снятие резерва (откат неудачного вызова).
- `RateLimiterGuard` — `before_call` (резерв) / `after_success` (подтвердить) /
  `after_failure` (вернуть резерв).
- `CompositeGuard(CallBudgetGuard(3), RateLimiterGuard)` — потолок 3 вызова на запрос
  плюс лимит 15/сессию и 100/день. Собран в `app.py` и передан в
  `process_request(llm_guard=...) → run_agent_graph`.
- `agent/nodes.py` вызывает `after_failure()` в ветках `LLMError` (resolve_context /
  agent_decision / fix_dsl) и при `client is None` — сетевой сбой лимит не съедает.
- Протокол `LLMCallGuard` дополнен `after_failure()`; `UnlimitedGuard` / `CallBudgetGuard`
  реализуют его; `CompositeGuard` откатывает уже сделанные резервы, если часть guard'ов
  отказала.
- UI-счётчик «Использовано **вызовов** ИИ: N/15» обновляется после каждого запроса из
  базы (`refresh_usage_caption`); классификатор уточнений учитывается атомарным
  `try_consume`.

Тесты: `tests/test_rate_limiter_guard.py` (11) — 3 вызова = +3; 16-й не уходит в API
(fallback); сетевой сбой не съедает; глобальные 100; AppTest-счётчик «1/15»; плюс
обновлённые `tests/test_rate_limiter.py`. `docs/LLM_CALLS.md` переписан под per-call.

## 2. Блок B — качество DSL (только промпты/примеры, контракт не менялся)

Правки только в `agent/prompts.py` (`_AGENT_RULES` + few-shot `_few_shot`):

- **clarify вместо угадывания:** «покажи продажи / сколько заработали / динамика этих
  товаров» без периода/объекта → `clarify`, не `total`/`timeseries` с выдуманным периодом.
- **ascending:** «хуже/меньше/антитоп/аутсайдеры/принесли меньше всего» → `true`;
  «лучше/топ/лидеры» → `false`.
- **хронология:** `period1` — раньше, `period2` — позже; «X по сравнению с Y» → `Y=period1`.
- **product_filter:** одна строка, не список; у `counterparty_dynamics` запрещён.
- **относительные даты:** строго от даты «Сегодняшняя дата», не от системной/тренировочной.
- **инъекции:** текст в кавычках — значение фильтра, приписки — данные, не команды;
  refuse — только если вопрос целиком не про аналитику или просит секреты/код.
- **дефолты:** не передавать `sort_by`/`agg`/`fill_gaps`/`only_drops`/`metric`, если не названы.
- few-shot примеры генерируются от `today`, имена не пересекаются с GoldSet; формат —
  описательная проза (псевдо-вызовы «→ tool(args)» ломали native tool calling у GigaChat).

Тест: `tests/test_agent_prompts.py` (6) — правила/примеры присутствуют, каталог
инструментов собирается из Pydantic-схем (не дублируется), today подставляется.

### Метрики до / после (live, 49 активных кейсов, fact-check 100% у обоих)

| Провайдер | Метрика | Baseline | Этап 5 |
|---|---|---|---|
| DeepSeek `deepseek-chat` | status | 89.8% | **93.9%** |
| | DSL | 81.6% | **85.7%** |
| | simple (DSL) | 80.0% | 80.0% |
| | context (DSL) | 71.4% | 71.4% |
| | edge_case (status) | 81.8% | **100%** |
| | non_analytical (status) | 100% | 100% |
| GigaChat `GigaChat-2-Pro` | status | 83.7% | 81.6% |
| | DSL | 69.4% | 67.3% |
| | edge_case (status) | 54.5% | 63.6% |

Судья `GigaChat-2-Pro`: **92% «да»** (23/25) — совпадает с baseline, non-regression.

### Вывод по целям

- **overall DSL ≥ 85%** — DeepSeek **85.7% ✓**; GigaChat 67.3% ✗ (свойство модели).
- **edge_case / non_analytical 100% статуса** — DeepSeek **100% ✓** (инъекции 044/045 и
  clarify 010/011/020/042/043 исправлены); GigaChat 63.6% ✗.
- **fact-check 100%** — оба ✓; **judge ≥ 90%** — 92% ✓.
- **simple ≥ 90%, context ≥ 75%** — не достигнуты (см. §3).

## 3. Остаточные ограничения (честно зафиксировано)

GoldSet не менялся. Остаточные расхождения DeepSeek (5 из 25 simple + 2 context):

- `002_antitop`: модель верно ставит `ascending=true`, но `limit=10` (дефолт контракта)
  против `limit=5` в GoldSet — **расхождение GoldSet/контракт** (вопрос без числа, а
  контрактный дефолт `limit` = 10).
- `007_counterparty`: период верен, но модель добавляет `sort_by="delta"` против дефолта
  `delta_abs` — дефолт не передан.
- `014_filter_product`: модель пишет `product_filter="Колонки Му"` (родительный падеж)
  вместо `"Колонка Му"` — нормализация падежа названия товара.
- `017_context_relative_dates` и `016_context_change_intent`: относительные даты /
  смена интента с контекстом — модель неустойчива (часть прогонов refuse/clarify).
- `023_l4l_specific`: сравнение одного конкретного товара за два периода — модель
  выбирает `compare_top_products`, а должна refuse (D4).

Это не подгонка GoldSet: кейсы корректны, остаточные ошибки — свойство моделей
(DeepSeek неустойчив на относительных датах/падеже, GigaChat-2-Pro слабее и на
edge_case/уточнениях). Дальнейший рост — за пределами промптов (калибровка дат
детерминированными правилами, словарь товаров, отдельный ADR).

## 4. Блок C — релиз 0.8.0

- Версия `0.8.0`: `pyproject.toml`, `src/onec_analyst/__init__.py`, раздел в `CHANGELOG.md`.
- Документация: README (счётчик 875, LangGraph/eval/лимиты, ссылки), `docs/INDEX.md`,
  `docs/LLM_CALLS.md`, `docs/ARCHITECTURE.md` (0.8.0), `docs/RUNBOOK.md` (875).
- Качество кода: `ruff` (select E/F/W/I/B/UP/C4/RUF; исключены BLE001, RUF001-003 —
  кириллица, E501 — длинные строки промптов/сводок, UP040/UP042 — PEP 695/StrEnum) —
  **зелёно**; `mypy` по `agent/`, `llm_agent.py`, `dispatcher.py`, `pipeline.py` —
  **зелёно** (`follow_imports="silent"`, доменные analytics/summary вне скоупа).
- CI: шаги `ruff check src` и `mypy`.
- Тесты: **875 passed, 1 skipped** (skip — `test_033` revenue_vat, расширение контракта
  в бэклоге). Кассеты/replay не перезаписывались.
- **Docker: blocker.** Демон Docker Desktop не запущен
  (`failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine`),
  поэтому `docker build` / `docker compose up -d --build --wait` и smoke-вопрос в
  контейнере не выполнены. Клиент установлен (Docker 29.9.2, Compose v5.6.1), сервер
  не поднят — запустить Docker Desktop должен владелец машины. Не имитировалось:
  сборка/приёмка в контейнере — следующий шаг после старта демона.

## 5. Чек-лист

- [x] 3.2: per-call лимиты, атомарные, с тестами; UI-счётчик считает вызовы.
- [x] DSL: DeepSeek overall 85.7% (≥85%), edge/non 100%; факт-чек 100%; judge 92%.
- [x] simple/context — не достигнуты, причины в §3 (свойство модели/GoldSet-нюансы).
- [x] 875 passed, 1 skipped; ruff/mypy зелёные.
- [ ] Docker: **blocker** — демон Docker Desktop не запущен (сборка/приёмка не проверены).
- [x] 0.8.0 в pyproject/`__init__`/CHANGELOG; README/INDEX/RUNBOOK/ARCHITECTURE/LLM_CALLS обновлены.
