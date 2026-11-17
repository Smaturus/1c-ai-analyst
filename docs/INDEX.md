# Навигация по документации

Точка входа для читателя репозитория. Актуальная картина продукта — **версия 0.7.0**.

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

## 4. Быстрые факты (0.7.0)

- Демо: `sample_data.csv` — 600 строк, 12 товаров, 6 контрагентов, 2 группы, 2023–2024, 10 % возвратов.
- Контрольные суммы демо: выручка **99 708 100,00 ₽**, количество **5 315** шт.
- Проверки: **319** pytest; `smoke_test.py` — PASSED; `smoke_llm.py` — 92 проверки.
- 7 интентов; фильтры `product_filter` / `group_filter` / `min_base_revenue`; rate limiting демо; graceful degradation.
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
