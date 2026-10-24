# ИИ-ассистент для анализа данных 1С (MVP)

[![CI](https://github.com/Smaturus/1c-ai-analyst/actions/workflows/ci.yml/badge.svg)](https://github.com/Smaturus/1c-ai-analyst/actions/workflows/ci.yml)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Инструмент, который отвечает на вопросы к выгрузке из 1С на естественном языке. **LLM не считает числа**: она переводит вопрос в строгий JSON-DSL, а расчёты выполняет pandas. Результат — таблица, график и текстовое объяснение.

**Статус:** ✅ шаг 1 (загрузчик + 5 интентов + DSL), источник 1С, шаг 2 (Streamlit UI + Pydantic + LLM-слой) и шаг 3 (UX таблиц/ввода, графики Altair, pytest/CI, относительные даты) — готовы. UI принят через Streamlit `AppTest` и ручную проверку LLM (DeepSeek); тесты — `pytest`, CI на Python 3.12.

## Как это работает

```mermaid
flowchart LR
    Q[Вопрос пользователя] --> LLM[LLM: вопрос → JSON-DSL]
    LLM --> D[dispatch_intent]
    D --> A[analytics.py: pandas]
    DATA[(Витрина: Excel или 1С CSV)] --> A
    A --> R[Таблица / график / объяснение]
```

**Пять интентов:** топ/антитоп товаров, динамика контрагентов, временной ряд, срез по товарным группам, общая сумма за период.

## Быстрый старт

### Проверка на Excel-выгрузке (без 1С)

```powershell
cd D:\1C_data_assistant
.venv\Scripts\python.exe -X utf8 smoke_test.py
```

Ожидаемо: `SMOKE TEST PASSED` (12 разделов, 59 проверок).

```python
from data_loader import load_and_clean_1c_report
from analytics import get_top_bottom_products

df = load_and_clean_1c_report("тест.xlsx")
print(get_top_bottom_products(df, limit=5))
```

### Выгрузка из 1С (база `C:\1C\utdemo`)

Требуется Python с `pywin32` (в этой машине — системный Python 3.14):

```powershell
& 'C:\Users\smatu\AppData\Local\Programs\Python\Python314\python.exe' `
  D:\1C_data_assistant\onec_source.py `
  --base "C:\1C\utdemo" --out-dir D:\1C_data_assistant\1c_export `
  --start 2022-01-01 --end 2024-12-31
```

```python
from onec_source import load_vitrina

df = load_vitrina("1c_export/sales_1c.csv")   # date, product, counterparty, quantity, revenue, product_group
```

Готовая выгрузка уже лежит в [`1c_export/sales_1c.csv`](1c_export/sales_1c.csv) — можно работать без 1С.

### UI и LLM-слой (шаги 2–3)

Самый простой запуск на Windows — дважды кликнуть по [`start_app.bat`](start_app.bat): скрипт сам создаст `.venv`, поставит зависимости и откроет браузер.

Вручную:

```powershell
cd D:\1C_data_assistant
python -m pip install -r requirements.txt      # pandas, streamlit, pydantic, pywin32
streamlit run app.py
```

По умолчанию приложение работает **без ключа LLM** — в офлайн-режиме детерминированных правил. Для LLM задайте переменные окружения (см. [`.env.example`](.env.example)):

```powershell
$env:LLM_PROVIDER = "deepseek"          # openai | deepseek | ollama | none
$env:LLM_API_KEY  = "sk-..."
$env:LLM_MODEL    = "deepseek-chat"
streamlit run app.py
```

Офлайн-правила понимают формулировки без слова «продажи» — например, «Динамика по месяцам за 2023», «Итог за 2024», «Лидеры по выручке за 2024».

В системный промпт передаётся **текущая дата** и **Few-Shot примеры** с датами, рассчитанными от неё (прошлый/позапрошлый год, март позапрошлого года, сравнение двух относительных лет) — это закрепляет арифметику сдвига лет. Офлайн-правила понимают те же формулировки (включая «в марте позапрошлого года» и сравнение «прошлый год по сравнению с позапрошлым»), поэтому относительные даты работают и без LLM.

В интерфейсе: поле вопроса с кнопкой очистки **✖️** (форма не отправляется, текст сбрасывается сразу), выбор источника (1С CSV / Excel / загрузка файла), переключатель **«Использовать LLM»** (без ключа заблокирован; при выключении вопросы разбирают офлайн-правила без вызовов API), качество данных в сайдбаре, вопрос → LLM (или правила) → Pydantic-валидация → `dispatch_intent` → таблица с русскими заголовками и сортировкой по клику (числа и даты остаются числами и датами), график Altair с русскими осями (горизонтальные бары для топ/динамики, линия для временного ряда), сводка и свёрнутый блок «Технические детали» с JSON-DSL.

Проверка LLM-слоя отдельно (нужен pydantic): [`smoke_llm.py`](smoke_llm.py) — 86 проверок.

## Как запустить тесты

Unit-тесты лежат в [`tests/`](tests/) и не требуют реальных выгрузок: синтетическая витрина собирается в [`tests/conftest.py`](tests/conftest.py). Покрыто 152 теста — расчёты всех пяти интентов, отрисовка таблиц и графиков, статическая проверка app.py (нет «голых» строк), разбор вопросов офлайн-планировщиком (включая регрессию «год попал в limit» и фильтр `only_drops`).

```powershell
cd D:\1C_data_assistant

# 1. Активировать окружение (Windows cmd/bat; в PowerShell — .venv\Scripts\Activate.ps1
#    при разрешённой политике выполнения, либо start_app.bat, который делает всё сам)
call .venv\Scripts\activate.bat

# 2. Поставить зависимости и pytest
python -m pip install -r requirements.txt
python -m pip install pytest

# 3. Запустить тесты
python -m pytest
```

Ожидаемо: `152 passed`. Настройки pytest — в [`pytest.ini`](pytest.ini) (`testpaths = tests`, `pythonpath = .`), при упаковке они же продублированы в [`pyproject.toml`](pyproject.toml).

Быстрый прогон отдельных файлов:

```powershell
python -m pytest tests/test_analytics.py -q   # расчёты на синтетике
python -m pytest tests/test_planner.py -q     # вопрос → JSON-DSL
```

Те же тесты прогоняются в CI: [`.github/workflows/ci.yml`](.github/workflows/ci.yml) на Python 3.12 при push в `main` и в pull request.

## Что уже есть

| Модуль | Назначение |
|---|---|
| [`data_loader.py`](data_loader.py) | ETL Excel-выгрузки «Универсального отчёта» |
| [`analytics.py`](analytics.py) | 5 интентов, сравнение периодов, диагностика, JSON-DSL |
| [`onec_source.py`](onec_source.py) | коннектор 1С: выгрузка в CSV и загрузка витрины |
| [`llm_agent.py`](llm_agent.py) | Pydantic-модели DSL, промпт, LLM-клиент, офлайн-правила |
| [`app.py`](app.py) | Streamlit UI: источник, вопрос, таблица, графики Altair, JSON-DSL |
| [`tests/test_analytics.py`](tests/test_analytics.py) | pytest: расчёты 5 интентов на синтетической витрине (26) |
| [`tests/test_planner.py`](tests/test_planner.py) | pytest: офлайн-правила «вопрос → JSON-DSL» (81) |
| [`tests/test_app_display.py`](tests/test_app_display.py) | pytest: заголовки, форматы, сортируемость таблиц (21) |
| [`tests/test_app_planner.py`](tests/test_app_planner.py) | pytest: переключатель LLM (AppTest, 7) |
| [`tests/test_app_question.py`](tests/test_app_question.py) | pytest: поле вопроса и очистка (AppTest, 5) |
| [`smoke_test.py`](smoke_test.py) | сквозной прогон на реальных данных |
| [`smoke_llm.py`](smoke_llm.py) | самопроверка LLM-слоя (Pydantic) |

**Проверенные цифры:**

- Excel: 821 строка, 96 товаров, 40 контрагентов, 250 строк без даты (24,4 % выручки).
- 1С (2022–2024): 2 568 записей, **0 строк без даты**, 92 товара, 40 контрагентов, 27 групп у товаров (61 в иерархии), 16 групп в периоде.
- Контрольные суммы совпадают у обоих источников: **19 313 936,63 ₽** без НДС, **22 957 182,97 ₽** с НДС, **10 878** шт.
- Временной ряд: 2022 — 5 152 624,77; 2023 — 12 443 435,76; 2024 — 1 717 876,10 ₽.

## Ключевые методические решения

1. **Выручка — без НДС** (по умолчанию). С НДС доступна режимом `revenue_mode="gross"`. Почему — [ADR 0001](docs/adr/0001-revenue-net.md).
2. **Источник для периодов — 1С**, Excel — fallback. Почему — [ADR 0002](docs/adr/0002-onec-direct-source.md).
3. **Товарные группы — иерархия справочника «Номенклатура»**. Почему — [ADR 0003](docs/adr/0003-product-groups.md).
4. **Граница LLM → код — JSON-DSL + `dispatch_intent`**. Почему — [ADR 0004](docs/adr/0004-llm-json-dsl.md).
5. **Выгрузка 1С отдельным шагом, CSV как контракт**. Почему — [ADR 0005](docs/adr/0005-onec-export-split.md).

## Структура

```
D:\1C_data_assistant
├─ data_loader.py            # ETL Excel
├─ analytics.py              # 5 интентов + DSL
├─ onec_source.py            # 1С → CSV → pandas
├─ llm_agent.py              # Pydantic DSL + промпт + LLM/правила
├─ app.py                    # Streamlit UI + графики Altair
├─ start_app.bat             # запуск в один клик (venv + зависимости + браузер)
├─ smoke_test.py             # проверка данных/интентов на реальной выгрузке
├─ smoke_llm.py              # проверка LLM-слоя (Pydantic)
├─ tests/
│  ├─ conftest.py            # синтетическая витрина для тестов
│  ├─ test_analytics.py      # pytest: расчёты интентов (26)
│  ├─ test_planner.py        # pytest: правила → JSON-DSL (81)
│  ├─ test_app_display.py    # pytest: таблицы, форматы, графики (31)
│  ├─ test_app_planner.py    # pytest: переключатель LLM (7)
│  ├─ test_app_question.py   # pytest: поле вопроса (5)
│  └─ test_app_static.py     # pytest: AST-регресс magic-строк app.py (2)
├─ pyproject.toml            # упаковка (PEP 621), зависимости, настройки pytest
├─ pytest.ini                # конфигурация pytest
├─ .github/workflows/ci.yml  # CI: pytest на Python 3.12
├─ LICENSE                   # MIT
├─ requirements.txt          # runtime-зависимости
├─ .env.example              # настройки LLM
├─ тест.xlsx                 # исходная выгрузка
├─ 1c_export/                # выгрузка 1С (sales_1c.csv, nomenclature_1c.csv)
├─ docs/
│  ├─ REPORT.md              # итоговый отчёт
│  ├─ REPORT_STEP2.md        # отчёт об инкременте шага 2
│  ├─ REPORT_STEP3.md        # отчёт об инкременте шага 3
│  ├─ UI_ACCEPTANCE.md       # запуск UI и контрольные вопросы
│  ├─ ARCHITECTURE.md        # архитектура
│  ├─ DATA_CONTRACT.md       # контракт витрины и CSV
│  ├─ RUNBOOK.md             # запуск и эксплуатация
│  ├─ ITS_ACCESS_NOTES.md    # доступ к ИТС
│  ├─ INDEX.md               # навигация по документам
│  └─ adr/                   # архитектурные решения
├─ progress.md               # журнал и план
├─ Промпт_для_нового_чата.md # передача контекста
└─ CHANGELOG.md
```

## Документация

- [docs/REPORT.md](docs/REPORT.md) — что сделано, цифры, проверки, ограничения.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — компоненты и потоки данных.
- [docs/DATA_CONTRACT.md](docs/DATA_CONTRACT.md) — схема витрины и CSV.
- [docs/RUNBOOK.md](docs/RUNBOOK.md) — команды, типовые ошибки, 1С и лицензия.
- [docs/UI_ACCEPTANCE.md](docs/UI_ACCEPTANCE.md) — запуск UI и 5 контрольных вопросов с ожидаемыми интентами.
- [docs/REPORT_STEP2.md](docs/REPORT_STEP2.md) — отчёт об инкременте шага 2.
- [docs/REPORT_STEP3.md](docs/REPORT_STEP3.md) — отчёт об инкременте шага 3: UX, pytest/CI, относительные даты.
- [docs/ITS_ACCESS_NOTES.md](docs/ITS_ACCESS_NOTES.md) — ИТС: что доступно, что нет.
- [docs/adr/](docs/adr/) — решения с обоснованием.
- [progress.md](progress.md) — статус и план.
- [Промпт_для_нового_чата.md](Промпт_для_нового_чата.md) — быстрый старт в новом чате.

## Ограничения

- В Excel-выгрузке 24,4 % выручки без даты — для периодов нужен источник 1С.
- Имена товаров в Excel обрезаны; канонические — из 1С.
- Документация ИТС по продуктам недоступна (договор `developer.1c.ru`), см. [заметки](docs/ITS_ACCESS_NOTES.md).
- `pywin32` отсутствует в рабочем Python — выгрузка 1С запускается отдельным интерпретатором.
- UI проверялся локальным запуском Streamlit (Windows, Python 3.14); CI прогоняет только unit-тесты на Python 3.12 — без браузера и без выгрузки из 1С.

## Лицензия и данные

Код распространяется по лицензии [MIT](LICENSE). Демонстрационные данные (`тест.xlsx`, `1c_export/`) используются только для примеров; копия базы 1С и временные профили браузера в репозитории не хранятся.
