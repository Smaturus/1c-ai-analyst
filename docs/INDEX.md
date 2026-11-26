# Навигация по документации

Точка входа для читателя репозитория. Актуальная картина продукта — **версия 0.8.0**.

## 1. С чего начать

| Роль | Порядок чтения |
|---|---|
| Быстрый обзор | [../README.md](../README.md) → [ARCHITECTURE.md](ARCHITECTURE.md) |
| Разработчик | [ARCHITECTURE.md](ARCHITECTURE.md) → [DATA_CONTRACT.md](DATA_CONTRACT.md) → [adr/](adr/) → код |
| Эксплуатация / запуск | [RUNBOOK.md](RUNBOOK.md) → [UI_ACCEPTANCE.md](UI_ACCEPTANCE.md) |
| Ревьюер решений | [adr/](adr/) → [ARCHITECTURE.md](ARCHITECTURE.md) → `smoke_test.py` / pytest |

## 2. Актуальные документы

| Документ | Содержание |
|---|---|
| [../README.md](../README.md) | Обзор, быстрый старт, возможности |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Архитектура приложения: компоненты, потоки, ограничения MVP |
| [ARCHITECTURE-SOLUTION.md](ARCHITECTURE-SOLUTION.md) | Архитектура решения: MCP-фасад, C4/ArchiMate, 11 схем |
| [architecture/](architecture/) | Исходники `.dot` и рендеры схем (SVG/PNG) |
| [DATA_CONTRACT.md](DATA_CONTRACT.md) | Схема витрины, инварианты, нетто-выручка |
| [RUNBOOK.md](RUNBOOK.md) | Запуск, выгрузка 1С, типовые ошибки |
| [UI_ACCEPTANCE.md](UI_ACCEPTANCE.md) | Контрольные вопросы и ожидания UI |
| [LLM_CALLS.md](LLM_CALLS.md) | Все LLM-вызовы: точки, лимиты §4.2, аудит, fallback |
| [EVAL.md](EVAL.md) | Eval-слой: кассеты, replay, live, judge, метрики и пороги |
| [DECISIONS.md](DECISIONS.md) | Журнал принятых решений (канон спринта S1) |
| [PUBLIC-DATA-POLICY.md](PUBLIC-DATA-POLICY.md) | Политика публичных данных: что публикуется, что нет, как проверяется |
| [SPRINT-S1.md](SPRINT-S1.md) | Спринт «Канонизация»: истории, AC, итоги |
| [TZ_64_patched_instruction.md](TZ_64_patched_instruction.md) | Перечень правок ТЗ 6.4 «было → стало» |
| [VIBECODING_EXPERIENCE.md](VIBECODING_EXPERIENCE.md) | Ретроспектива сессии: методика, код, процесс |
| [REPORT_LANGGRAPH_STAGE2.md](REPORT_LANGGRAPH_STAGE2.md) | Отчёт этапа 2: LangGraph-агент, статусы, память, лимиты, тесты |
| [REPORT_LANGGRAPH_STAGE25.md](REPORT_LANGGRAPH_STAGE25.md) | Отчёт этапов 2.5/2.5a: GigaChat через `langchain-gigachat`, версии, adapter, явный выбор модели, тесты |
| [REPORT_LANGGRAPH_STAGE3.md](REPORT_LANGGRAPH_STAGE3.md) | Отчёт этапа 3: нативные tool calls и аудит шагов графа (исправления 3.1) |
| [REPORT_LANGGRAPH_STAGE4.md](REPORT_LANGGRAPH_STAGE4.md) | Отчёт этапа 4: eval-слой — кассеты, replay, live, judge |
| [REPORT_LANGGRAPH_STAGE5.md](REPORT_LANGGRAPH_STAGE5.md) | Отчёт этапа 5: качество DSL, per-call лимиты (§4.2), релиз 0.8.0 |
| [REPORT_PROVIDERS_GOLDSET.md](REPORT_PROVIDERS_GOLDSET.md) | Сравнение DeepSeek и Ollama Qwen на внешнем GoldSet (50 кейсов): 42/50 и 34/50 |
| [intent_schema.json](intent_schema.json) | Схема 7 интентов для LLM/интеграций |
| [../CHANGELOG.md](../CHANGELOG.md) | История версий (Keep a Changelog) |

## 3. Архитектурные решения (ADR)

| ADR | Решение |
|---|---|
| [0001](adr/0001-revenue-net.md) | Выручка без НДС |
| [0002](adr/0002-onec-direct-source.md) | 1С как основной источник периодов |
| [0003](adr/0003-product-groups.md) | Товарные группы = иерархия номенклатуры |
| [0004](adr/0004-llm-json-dsl.md) | JSON-DSL и `dispatch_intent` |
| [0005](adr/0005-onec-export-split.md) | ETL отделён от UI (Data Snapshot) |
| [0006](adr/0006-llm-providers.md) | Провайдеры LLM и офлайн-правила |
| [0007](adr/0007-graceful-degradation.md) | Graceful degradation при уточнениях |
| [0008](adr/0008-compare-tops-intent.md) | Сравнение топов (L4L) |
| [0009](adr/0009-mcp-facade.md) | MCP-фасад над JSON-DSL |
| [0010](adr/0010-export-timestamp.md) | Дата данных из метаданных выгрузки |
| [0011](adr/0011-answer-contract-version.md) | Версия контракта ответа (предложено) |
| [0012](adr/0012-gigachat-provider.md) | GigaChat как провайдер планировщика (tool calling, предложено) |
| [0013](adr/0013-langgraph-hybrid-contract.md) | Гибридный LangGraph-агент: канонический DSL, явный UNKNOWN, политика eval (принято) |
| [0014](adr/0014-gigachat-langchain.md) | GigaChat в LangGraph-агенте через `langchain-gigachat` (принято) |
| [0015](adr/0015-native-tools-audit.md) | Нативные tool calls OpenAI-совместимых провайдеров и аудит шагов графа (принято) |
| [0016](adr/0016-session-journal-notion-export.md) | Журнал аналитических сессий в SQLite и экспорт в Notion (принято) |
| [0017](adr/0017-1c-export-sqlite.md) | Выгрузка 1С в отдельную SQLite-БД: архив и SQL-слой (принято) |
| [0018](adr/0018-testing-artifacts-sqlite.md) | Тестовые артефакты (программа, результаты, кассеты) в analytics.db (принято) |

## 4. Быстрые факты (0.8.0)

- Демо: `sample_data.csv` — 600 строк, 12 товаров, 6 контрагентов, 2 группы, 2023–2024, 10 % возвратов.
- Контрольные суммы демо: выручка **99 708 100,00 ₽**, количество **5 315** шт.
- Проверки: **1214** pytest (2 xfail strict; 1212 passed); `smoke_test.py` — PASSED; `smoke_llm.py` — 95 проверок.
- Выгрузка 1С: CSV (источник) + `db/analytics.db` (архив/SQL-слой, <N> продаж и <N> позиций номенклатуры).
- Тестовые артефакты в той же БД: 57 кейсов программы, 285 строк результатов (5 моделей), 50 golden-кассет.
- Журнал сессий: `db/sessions.db` (fail-safe, вне git) + выгрузка в Notion скриптом `scripts/notion_export.py` (вне пути запроса).
- 7 интентов; LangGraph-агент + GigaChat через `langchain-gigachat`; native tools + аудит шагов; провайдеры: DeepSeek, OpenAI, Ollama, GigaChat.
- Per-call лимиты LLM (15/сессию, 100/день, §4.2); eval-слой (GoldSet, кассеты, live, judge).
- Реальные выгрузки 1С и секреты в репозиторий не входят.

## 5. История разработки

Дневник инкрементов (`docs/history/`, `progress.md`) ведётся **локально** и в репозиторий не публикуется (см. `.gitignore`).  
Актуальная спецификация продукта — [ARCHITECTURE.md](ARCHITECTURE.md); решения — [adr/](adr/); изменения версий — [CHANGELOG.md](../CHANGELOG.md).

## 6. Правила ведения документации

1. Факт о продукте изменился — обновить README / ARCHITECTURE / DATA_CONTRACT и запись в CHANGELOG.
2. Новое архитектурное решение — новый ADR (старые не переписываются).
3. Числа сверять с `pytest` / `smoke_test.py`; при расхождении источник истины — тест.
4. Секреты, пароли, копия базы 1С в документацию не попадают.
5. Дневниковые отчёты по инкрементам не коммитить: только локально (`docs/history/`, `progress.md`).
