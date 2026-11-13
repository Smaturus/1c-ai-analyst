# Архитектурные схемы

Исходники и рендеры архитектурных диаграмм решения (C4 + ArchiMate).

## Что здесь

| Файл | Нотация | Содержание |
|---|---|---|
| `01-business-architecture` | ArchiMate Business | акторы, сервисы, объекты |
| `02-system-context` | C4 L1 | контекст и граница системы |
| `03-system-containers` | C4 L2 | контейнеры и протоколы |
| `04-mcp-components` | C4 L3 | компоненты MCP-сервера |
| `05-adhoc-query-flow` | flowchart (последовательность шагов) | вопрос → срез → ответ |
| `06-refusal-flow` | flowchart (последовательность шагов) | отказ без вычислений |
| `07-returns-alert-flow` | flowchart (последовательность шагов) | cron → алерт возвратов |
| `08-data-model` | ER | витрина, meta, аудит |
| `09-deployment-view` | deployment | узлы, venv, gateway, файлы |
| `10-trust-zones` | trust boundaries | зоны доверия и контроли |
| `11-archimate-layers` | ArchiMate 3.2 | слои Business → Application → Technology |
| `12-configuration-view` | configuration | настройки Hermes и tool surface |
| `13-dataset-switch-flow` | flow | UC-3: смена витрины |

Для каждой схемы три артефакта: `src/<name>.dot` (источник), `<name>.svg`
(вектор), `<name>.png` (200 dpi для вставки в чат/презентацию).

## Как собрать

```powershell
# Graphviz: https://graphviz.org (winget install Graphviz.Graphviz)
python -X utf8 docs/architecture/build_diagrams.py
```

Скрипт сам находит `dot` (PATH или `C:\Program Files\Graphviz\bin\dot.exe`),
генерирует `.dot` и рендерит `.svg` + `.png`.

## Оформление

- Единая палитра C4: человек `#08427B`, наша система `#1168BD`,
  контейнер `#438DD5`, компонент `#85BBF0`, хранилище `#2E6DA4`,
  внешнее `#7D8B99`; ArchiMate — жёлтый business, голубой application.
- Шрифт Segoe UI, скруглённые карточки с типом элемента, названием,
  описанием и технологией; на каждой схеме заголовок и подпись источника.
- Схемы встроены в `docs/ARCHITECTURE-SOLUTION.md` (разделы 4–7, 9 и
  приложение A).

## Правки

- Содержание и связи — в `build_diagrams.py` (функции `*_dot`), затем
  пересборка. Не редактируйте `.dot` вручную: следующий запуск перезапишет.
- Для точной ручной компоновки можно перейти на `neato -n` с координатами
  или экспортировать в Archi/draw.io; текущий вариант — детерминированная
  авто-раскладка Graphviz.
