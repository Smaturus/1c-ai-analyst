"""Сборка профессиональных архитектурных схем (Graphviz / C4 / ArchiMate).

Источник истины — этот скрипт: он генерирует `.dot` и рендерит `.svg` + `.png`
(200 dpi) в `docs/architecture/`. Запуск::

    python -X utf8 docs/architecture/build_diagrams.py

Требуется Graphviz (`dot` в PATH или C:\\Program Files\\Graphviz\\bin\\dot.exe).
Стиль: единая палитра C4, шрифт Segoe UI, заголовок и подпись на каждой схеме.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
SRC = OUT / "src"

# --- палитра C4 / ArchiMate -------------------------------------------------
C = {
    "person": ("#08427B", "#FFFFFF", "#C7D7EA"),
    "system": ("#1168BD", "#FFFFFF", "#C7D7EA"),
    "external": ("#7D8B99", "#FFFFFF", "#DDE3E9"),
    "container": ("#438DD5", "#FFFFFF", "#DCE9F5"),
    "component": ("#85BBF0", "#1F2933", "#EAF4FC"),
    "store": ("#2E6DA4", "#FFFFFF", "#DCE9F5"),
    "tech": ("#C9E7B7", "#1F2933", "#EEF7EA"),
    "business": ("#FFF2B2", "#1F2933", "#FBF5D7"),
    "app": ("#B5E0F5", "#1F2933", "#EAF4FC"),
    "data": ("#D9C2E9", "#1F2933", "#F3ECF8"),
    "risk": ("#F4C7C3", "#1F2933", "#FBE7E5"),
}
MUTED = "#5A6B7B"
EDGE = "#5A6B7B"
INK = "#1F2933"


def label(kind: str, name: str, desc: str = "", tech: str = "", dark: bool = True) -> str:
    """HTML-метка C4: тип / имя / описание / технология."""
    name_color = "#FFFFFF" if dark else INK
    sub_color = "#D6E4F0" if dark else MUTED
    parts = [
        '<TABLE BORDER="0" CELLBORDER="0" CELLSPACING="0" CELLPADDING="3">',
        f'<TR><TD ALIGN="LEFT"><FONT POINT-SIZE="8" COLOR="{sub_color}">{kind}</FONT></TD></TR>',
        f'<TR><TD ALIGN="LEFT"><FONT COLOR="{name_color}"><B>{name}</B></FONT></TD></TR>',
    ]
    if desc:
        parts.append(
            f'<TR><TD ALIGN="LEFT"><FONT POINT-SIZE="9" COLOR="{sub_color}">{desc}</FONT></TD></TR>'
        )
    if tech:
        parts.append(
            f'<TR><TD ALIGN="LEFT"><FONT POINT-SIZE="8" COLOR="{sub_color}">[{tech}]</FONT></TD></TR>'
        )
    parts.append("</TABLE>")
    return "<" + "".join(parts) + ">"


def node(name: str, kind: str, title: str, desc: str = "", tech: str = "", shape: str = "box") -> str:
    fill, font, sub = C[kind]
    dark = font == "#FFFFFF"
    return (
        f'  "{name}" [shape="{shape}", fillcolor="{fill}", fontcolor="{font}", '
        f'label={label(kind, title, desc, tech, dark=dark)}];\n'
    )


def edge(src: str, dst: str, text: str = "", style: str = "solid", arrow: str = "vee", extra: str = "") -> str:
    parts = [f'  "{src}" -> "{dst}"']
    attrs = [f'style="{style}"', f'arrowhead="{arrow}"']
    if text:
        attrs.append(f'label="{text}"')
    if extra:
        attrs.append(extra)
    parts.append(" [" + ", ".join(attrs) + "];\n")
    return "".join(parts)


def cluster(name: str, title: str, body: str, *, color: str = "#9AA7B4", dashed: bool = True) -> str:
    style = "rounded,dashed" if dashed else "rounded"
    return (
        f'  subgraph cluster_{name} {{\n'
        f'    label=<<FONT POINT-SIZE="10" COLOR="{MUTED}"><B>{title}</B></FONT>>;\n'
        f'    labeljust="l"; style="{style}"; color="{color}"; penwidth="1.1";\n'
        f'    fontname="Segoe UI"; margin="14";\n{body}  }}\n'
    )


def wrap(name: str, title: str, subtitle: str, body: str, *, rankdir: str = "LR", note: str = "") -> str:
    note_row = ""
    if note:
        note_row = (
            f'<TR><TD ALIGN="LEFT"><FONT POINT-SIZE="8" COLOR="{MUTED}">{note}</FONT></TD></TR>'
        )
    return (
        f'digraph "{name}" {{\n'
        f'  graph [rankdir="{rankdir}", bgcolor="white", pad="0.45", nodesep="0.55", '
        f'ranksep="0.85", fontname="Segoe UI", fontsize="11", labelloc="t", labeljust="l",\n'
        f'         label=<<TABLE BORDER="0" CELLBORDER="0" CELLSPACING="0" CELLPADDING="2">\n'
        f'           <TR><TD ALIGN="LEFT"><FONT POINT-SIZE="16"><B>{title}</B></FONT></TD></TR>\n'
        f'           <TR><TD ALIGN="LEFT"><FONT POINT-SIZE="10" COLOR="{MUTED}">{subtitle}</FONT></TD></TR>\n'
        f'           {note_row}\n'
        f'         </TABLE>>,\n'
        f'         dpi="200"];\n'
        f'  node [shape="box", style="rounded,filled", fontname="Segoe UI", fontsize="11", '
        f'penwidth="1.2", margin="0.10,0.06"];\n'
        f'  edge [fontname="Segoe UI", fontsize="9", color="{EDGE}", fontcolor="{INK}", '
        f'arrowsize="0.75", penwidth="1.1"];\n'
        f'{body}}}\n'
    )


def legend(items: list[tuple[str, str]]) -> str:
    """Легенду выносим в текст документа: в графе она ломает компоновку."""
    return ""


# --- 01. Бизнес-слой (ArchiMate) -------------------------------------------
def business_layer() -> str:
    body = (
        cluster(
            "actors",
            "Business Layer · Акторы",
            node("dir", "business", "Пользователь", "вопросы, решения")
            + node("ana", "business", "Аналитик", "витрина, сверка")
            + node("it", "business", "ИТ / ИБ", "доступы, канал"),
        )
        + cluster(
            "services",
            "Business Layer · Сервисы",
            node("sales", "business", "Аналитика продаж", "ответ на вопрос")
            + node("returns", "business", "Контроль возвратов", "ежедневный алерт")
            + node("accept", "business", "Сверка с 1С", "30 вопросов, подпись"),
        )
        + cluster(
            "objects",
            "Business Layer · Объекты",
            node("report", "business", "Отчёт о продажах", "таблица + провенанс")
            + node("refund", "business", "Возвраты", "строки, суммы")
            + node("protocol", "business", "Протокол сверки", "вердикты"),
        )
        + edge("dir", "sales", "UC-1, UC-2")
        + edge("dir", "returns", "UC-2")
        + edge("ana", "accept", "UC-5")
        + edge("it", "sales", "канал, доступ")
        + edge("sales", "report", "формирует")
        + edge("returns", "refund", "фиксирует")
        + edge("accept", "protocol", "фиксирует")
        + legend(
            [
                (C["business"][0], "бизнес-элемент (ArchiMate Business)"),
                ("#FFFFFF", "стрелка — serving / association"),
            ]
        )
    )
    return wrap(
        "business-layer",
        "Бизнес-архитектура: аналитика продаж 1С",
        "ArchiMate 3.2 · Business Layer · акторы, сервисы, объекты",
        body,
        rankdir="LR",
        note="Источник: docs/ARCHITECTURE-SOLUTION.md · build_diagrams.py",
    )


# --- 02. C4 Context ---------------------------------------------------------
def c4_context() -> str:
    body = (
        node("dir", "person", "Пользователь", "задаёт вопросы в чате")
        + node("ana", "person", "Аналитик", "владелец витрины и сверки")
        + node("hermes", "external", "Hermes Agent", "диалог, канал, cron", "Hermes / Python")
        + node("messenger", "external", "Мессенджер / почта", "Mattermost, Teams, e-mail")
        + node("llm", "external", "LLM-провайдер", "планирование диалога", "по выбору заказчика")
        + node("onec", "external", "1С:УТ", "регистр продаж", "COM")
        + cluster(
            "boundary",
            "Система: 1c-ai-analyst",
            node(
                "system",
                "system",
                "1c-ai-analyst",
                "детерминированные срезы продаж 1С",
            ),
        )
        + edge("dir", "hermes", "чат")
        + edge("hermes", "messenger", "доставка", "dashed")
        + edge("hermes", "llm", "HTTPS", "dashed")
        + edge("hermes", "system", "MCP / stdio")
        + edge("ana", "system", "Streamlit UI")
        + edge("onec", "system", "выгрузка CSV + meta")
        + legend(
            [
                (C["person"][0], "человек"),
                (C["system"][0], "наша система"),
                (C["external"][0], "внешняя система"),
                (C["container"][0], "контейнер внутри системы"),
            ]
        )
    )
    return wrap(
        "c4-context",
        "C4 · Уровень 1: контекст системы",
        "Кто пользуется, с чем интегрируется, где проходит граница ответственности",
        body,
        rankdir="LR",
        note="Источник: docs/ARCHITECTURE-SOLUTION.md · build_diagrams.py",
    )


# --- 03. C4 Container -------------------------------------------------------
def c4_container() -> str:
    body = (
        node("dir", "person", "Пользователь")
        + node("ana", "person", "Аналитик")
        + cluster(
            "hermes",
            "Hermes Agent (внешний агент)",
            node("gw", "external", "Gateway", "сессии, cron, каналы")
            + node("desk", "external", "Desktop / CLI", "чат")
            + node("cron", "external", "Cron scheduler", "проактивные задачи"),
        )
        + cluster(
            "sys",
            "1c-ai-analyst",
            node("mcp", "container", "MCP Server", "12 прикладных + 4 служебных", "Python, stdio")
            + node("streamlit", "container", "Streamlit UI", "глубокий разбор", "Python")
            + node("engine", "container", "Движок аналитики", "7 срезов, DSL", "pandas")
            + node("vitrina", "store", "Витрина", "CSV + meta", "файлы")
            + node("audit", "store", "Аудит", "JSONL", "файлы")
            + node("etl", "container", "ETL 1С → CSV", "выгрузка", "onec_source.py"),
        )
        + node("onec", "external", "1С:УТ", "источник", "COM")
        + edge("dir", "desk", "чат")
        + edge("ana", "streamlit", "браузер")
        + edge("desk", "gw", "IPC")
        + edge("cron", "gw", "задачи")
        + edge("gw", "mcp", "MCP / stdio")
        + edge("mcp", "engine", "dispatch_intent")
        + edge("engine", "vitrina", "чтение")
        + edge("mcp", "audit", "запись")
        + edge("etl", "vitrina", "запись")
        + edge("onec", "etl", "COM")
        + edge("streamlit", "engine", "Python API")
    )
    return wrap(
        "c4-container",
        "C4 · Уровень 2: контейнеры",
        "Процессы, хранилища и протоколы между ними",
        body,
        rankdir="LR",
    )


# --- 04. C4 Component -------------------------------------------------------
def c4_component() -> str:
    body = (
        cluster(
            "mcp",
            "MCP Server (stdio)",
            node("registry", "component", "Tool Registry", "12 прикладных + 4 служебных")
            + node("contract", "component", "Contract Formatter", "период, дата данных, отказ")
            + node("dataset", "component", "Dataset Store", "текущая витрина, allowlist")
            + node("executor", "component", "Slice Executor", "валидация → dispatch_intent")
            + node("totals", "component", "Derived Totals", "суммы/доли/мин/макс")
            + node("catalog", "component", "Dimension Catalog", "группы, контрагенты, товары")
            + node("alerts", "component", "Alert Engine", "окно, порог, калибровка")
            + node("auditlog", "component", "Audit Journal", "slice/error/refusal")
        )
        + node("engine", "container", "Движок аналитики", "analytics.dispatch_intent", "pandas")
        + node("vitrina", "store", "Витрина CSV + meta")
        + node("auditfile", "store", "audit.jsonl")
        + edge("registry", "executor", "вызов")
        + edge("registry", "alerts", "вызов")
        + edge("registry", "catalog", "вызов")
        + edge("dataset", "executor", "витрина")
        + edge("executor", "engine", "params")
        + edge("executor", "totals", "итоги")
        + edge("executor", "contract", "таблица")
        + edge("alerts", "engine", "окно")
        + edge("engine", "vitrina", "чтение")
        + edge("executor", "auditlog", "событие")
        + edge("alerts", "auditlog", "событие")
        + edge("auditlog", "auditfile", "append")
    )
    return wrap(
        "c4-component",
        "C4 · Уровень 3: компоненты MCP-сервера",
        "Из чего состоит адаптер и где проходит граница с движком",
        body,
        rankdir="LR",
    )


# --- 05. Flow: ad-hoc -------------------------------------------------------
def flow_adhoc() -> str:
    body = (
        node("dir", "person", "Пользователь", "вопрос")
        + node("llm", "external", "Hermes Agent", "выбор инструмента", "LLM")
        + node("tool", "container", "MCP tool", "total / group_dynamics", "stdio")
        + node("engine", "container", "Движок", "dispatch_intent", "pandas")
        + node("store", "store", "Витрина", "CSV + meta")
        + node("answer", "component", "Контрактный ответ", "период, данные, таблица")
        + edge("dir", "llm", "1 · вопрос")
        + edge("llm", "tool", "2 · MCP-вызов")
        + edge("tool", "engine", "3 · params")
        + edge("engine", "store", "4 · чтение")
        + edge("engine", "answer", "5 · DataFrame")
        + edge("answer", "llm", "6 · текст", "dashed")
        + edge("llm", "dir", "7 · ответ в чат", "dashed")
        + legend(
            [
                (C["person"][0], "пользователь"),
                (C["external"][0], "внешний агент"),
                (C["container"][0], "наш сервис"),
                ("#FFFFFF", "пунктир — ответное сообщение"),
            ]
        )
    )
    return wrap(
        "flow-adhoc",
        "Сценарий: ad-hoc вопрос",
        "Пользователь → агент → MCP-срез → движок → контрактный ответ",
        body,
        rankdir="LR",
    )


# --- 06. Flow: refusal ------------------------------------------------------
def flow_refusal() -> str:
    body = (
        node("q", "person", "Вопрос вне срезов", "например, «маржа»")
        + node("llm", "external", "Hermes Agent", "проверяет каталог", "LLM")
        + node("caps", "container", "describe_capabilities", "список срезов")
        + node("refuse", "component", "Отказ", "причина + доступное")
        + node("dir", "person", "Пользователь", "получает отказ")
        + edge("q", "llm", "1")
        + edge("llm", "caps", "2 · MCP")
        + edge("caps", "refuse", "3 · список")
        + edge("refuse", "llm", "4", "dashed")
        + edge("llm", "dir", "5 · без вычислений", "dashed")
        + legend([(C["component"][0], "контрактный отказ"), ("#FFFFFF", "цифры — порядок шагов")])
    )
    return wrap(
        "flow-refusal",
        "Сценарий: отказ вместо подмены",
        "Нет среза — нет числа: агент перечисляет доступное и не считает сам",
        body,
        rankdir="LR",
    )


# --- 07. Flow: alert --------------------------------------------------------
def flow_alert() -> str:
    body = (
        node("cron", "external", "Cron Hermes", "05:45 ежедневно")
        + node("llm", "external", "Hermes Agent", "задача «алерт возвратов»")
        + node("tool", "container", "returns_alert", "окно: пн = пт–вс")
        + node("engine", "container", "Alert Engine", "порог, калибровка")
        + node("store", "store", "Витрина", "данные за окно")
        + node("verdict", "component", "Вердикт", "алерт / тишина")
        + node("channel", "external", "Мессенджер", "доставка")
        + edge("cron", "llm", "1 · расписание")
        + edge("llm", "tool", "2 · MCP")
        + edge("tool", "engine", "3 · окно")
        + edge("engine", "store", "4 · суммы")
        + edge("engine", "verdict", "5 · решение")
        + edge("verdict", "llm", "6", "dashed")
        + edge("llm", "channel", "7 · сообщение / тишина", "dashed")
        + legend([(C["container"][0], "MCP-инструмент"), (C["component"][0], "серверное решение"), ("#FFFFFF", "пунктир — доставка")])
    )
    return wrap(
        "flow-alert",
        "Сценарий: проактивный алерт возвратов",
        "Cron → серверное решение по порогу → сообщение в канал",
        body,
        rankdir="LR",
    )


# --- 08. Data model ---------------------------------------------------------
def table_html(name: str, subtitle: str, fields: list[tuple[str, str, str]]) -> str:
    rows = "".join(
        f'<TR><TD ALIGN="LEFT"><FONT POINT-SIZE="8" COLOR="{MUTED}">{key or "&#160;"}</FONT></TD>'
        f'<TD ALIGN="LEFT"><FONT POINT-SIZE="9">{fname}</FONT></TD>'
        f'<TD ALIGN="LEFT"><FONT POINT-SIZE="8" COLOR="{MUTED}">{ftype}</FONT></TD></TR>'
        for key, fname, ftype in fields
    )
    return (
        '<<TABLE BORDER="0" CELLBORDER="0" CELLSPACING="0" CELLPADDING="3">'
        f'<TR><TD ALIGN="LEFT" COLSPAN="3" BGCOLOR="{C["data"][0]}"><FONT POINT-SIZE="10"><B>{name}</B></FONT></TD></TR>'
        f'<TR><TD ALIGN="LEFT" COLSPAN="3"><FONT POINT-SIZE="8" COLOR="{MUTED}">{subtitle}</FONT></TD></TR>'
        f"{rows}</TABLE>>"
    )


def data_model() -> str:
    vitrina = table_html(
        "VITRINA",
        "витрина продаж (CSV)",
        [
            ("", "date", "date"),
            ("", "product", "string"),
            ("", "counterparty", "string"),
            ("", "quantity", "float"),
            ("", "revenue", "float"),
            ("", "product_group", "string"),
        ],
    )
    meta = table_html(
        "EXPORT_META",
        "метаданные выгрузки (JSON)",
        [
            ("PK", "exported_at", "datetime"),
            ("", "rows", "int"),
            ("", "source", "string"),
            ("", "revenue_mode", "string"),
        ],
    )
    audit = table_html(
        "AUDIT_EVENT",
        "журнал вызовов (JSONL)",
        [
            ("PK", "ts", "datetime"),
            ("", "event", "enum"),
            ("", "intent", "string"),
            ("", "params", "json"),
            ("", "dataset", "string"),
            ("", "period", "json"),
            ("", "rows", "int"),
            ("", "shown", "int"),
            ("", "exported_at", "datetime"),
            ("", "triggered", "bool"),
            ("", "reason", "string"),
            ("", "details", "string"),
        ],
    )
    body = (
        f'  "vitrina" [shape="box", style="rounded,filled", fillcolor="white", color="#9AA7B4", penwidth="1.2", label={vitrina}];\n'
        f'  "meta" [shape="box", style="rounded,filled", fillcolor="white", color="#9AA7B4", penwidth="1.2", label={meta}];\n'
        f'  "audit" [shape="box", style="rounded,filled", fillcolor="white", color="#9AA7B4", penwidth="1.2", label={audit}];\n'
        + edge("vitrina", "meta", "1 : 1 · рядом с CSV", arrow="none", style="solid")
        + edge("vitrina", "audit", "1 : N · вызовы срезов", arrow="none", style="dashed")
    )
    return wrap(
        "data-model",
        "Модель данных",
        "Витрина, метаданные выгрузки и журнал аудита",
        body,
        rankdir="LR",
    )


# --- 09. Deployment ---------------------------------------------------------
def deployment() -> str:
    body = (
        node("dir", "person", "Пользователь", "чат")
        + node("ana", "person", "Аналитик", "браузер")
        + cluster(
            "host",
            "Windows host · рабочее место",
            cluster(
                "venv",
                "venv (Python 3.12+)",
                node("mcp", "container", "mcp_server", "12 + 4 инструмента", "stdio")
                + node("etl", "container", "ETL 1С → CSV", "onec_source.py")
                + node("streamlit", "container", "Streamlit UI", "разбор данных")
                + node("tests", "component", "pytest", "319 тестов"),
            )
            + cluster(
                "hermes",
                "Hermes Agent",
                node("gw", "external", "gateway", "сессии, каналы")
                + node("desk", "external", "Desktop / CLI")
                + node("cron", "external", "cron"),
            )
            + node("files", "store", "Файлы", "витрина + meta + audit")
            + node("onec", "external", "1С:УТ", "COM", "Windows"),
        )
        + node("msg", "external", "Мессенджер / почта")
        + node("llm", "external", "LLM provider")
        + edge("dir", "msg", "канал")
        + edge("ana", "streamlit", "браузер")
        + edge("msg", "gw", "API")
        + edge("desk", "gw", "IPC")
        + edge("cron", "gw", "задачи")
        + edge("gw", "mcp", "stdio")
        + edge("mcp", "files", "read/write")
        + edge("streamlit", "files", "чтение")
        + edge("onec", "etl", "COM")
        + edge("etl", "files", "запись")
        + edge("gw", "llm", "HTTPS", "dashed")
    )
    return wrap(
        "deployment",
        "Развёртывание",
        "Узлы, процессы, транспорт и файлы на рабочем месте",
        body,
        rankdir="TB",
    )


# --- 10. Security zones -----------------------------------------------------
def security_zones() -> str:
    body = (
        cluster(
            "shop",
            "Учётная система",
            node("onec", "external", "1С:УТ", "источник данных"),
        )
        + cluster(
            "trusted",
            "Доверенная зона: рабочее место",
            node("mcp", "container", "MCP Server", "read-only, allowlist")
            + node("engine", "container", "Движок", "без сети")
            + node("files", "store", "Витрина + аудит", "локальные файлы"),
        )
        + cluster(
            "agent",
            "Платформа агента (внешняя)",
            node("hermes", "external", "Hermes Agent", "toolset ограничен"),
        )
        + cluster(
            "external",
            "Внешние сервисы",
            node("llm", "external", "LLM-провайдер", "диалог агента")
            + node("msg", "external", "Мессенджер", "канал доставки"),
        )
        + edge("onec", "files", "CSV + meta (ETL)")
        + edge("hermes", "mcp", "MCP/stdio · allowlist")
        + edge("mcp", "files", "read-only · аудит")
        + edge("mcp", "engine", "dispatch_intent")
        + edge("hermes", "llm", "HTTPS · провайдер по выбору", "dashed")
        + edge("hermes", "msg", "API · allowlist", "dashed")
    )
    return wrap(
        "security-zones",
        "Зоны доверия и контроли",
        "Что где находится и чем ограничены переходы между зонами",
        body,
        rankdir="LR",
    )


# --- 11. ArchiMate layers ----------------------------------------------------
def archimate_layers() -> str:
    body = (
        cluster(
            "business",
            "Business Layer",
            node("user", "business", "Пользователь", "вопросы")
            + node("analyst", "business", "Аналитик", "сверка")
            + node("sales", "business", "Аналитика продаж", "сервис")
            + node("returns", "business", "Контроль возвратов", "сервис"),
        )
        + cluster(
            "application",
            "Application Layer",
            node("mcp", "app", "MCP Server", "12 + 4 инструмента")
            + node("engine", "app", "Движок аналитики", "7 срезов")
            + node("streamlit", "app", "Streamlit UI")
            + node("etl", "app", "ETL 1С → CSV")
            + node("audit", "app", "Аудит"),
        )
        + cluster(
            "technology",
            "Technology Layer",
            node("host", "tech", "Windows host")
            + node("python", "tech", "Python venv")
            + node("stdio", "tech", "MCP over stdio")
            + node("files", "tech", "Витрина · meta · audit"),
        )
        + edge("user", "mcp", "использует", "dashed", "onormal")
        + edge("analyst", "streamlit", "использует", "dashed", "onormal")
        + edge("sales", "mcp", "realized by", "dashed", "onormal")
        + edge("returns", "mcp", "realized by", "dashed", "onormal")
        + edge("mcp", "engine", "uses")
        + edge("streamlit", "engine", "uses")
        + edge("engine", "files", "reads")
        + edge("etl", "files", "writes")
        + edge("mcp", "stdio", "served by", "dashed", "onormal")
        + edge("mcp", "python", "runs on", "dashed", "onormal")
        + edge("etl", "python", "runs on", "dashed", "onormal")
        + edge("stdio", "host", "hosted by", "dashed", "onormal")
        + edge("python", "host", "hosted by", "dashed", "onormal")
        + edge("files", "host", "stored on", "dashed", "onormal")
    )
    return wrap(
        "archimate-layers",
        "ArchiMate: слои и реализации",
        "Business → Application → Technology: сервисы, компоненты, инфраструктура",
        body,
        rankdir="TB",
    )


# --- 12. Configuration view (Hermes profile) --------------------------------
def configuration_view() -> str:
    body = (
        cluster(
            "profile",
            "Hermes profile · config.yaml",
            node("mcp_srv", "app", "mcp_servers.1c-analyst", "command / args / cwd / env / timeout")
            + node("toolsets", "app", "platform_toolsets", "local / desktop")
            + node("disabled", "app", "agent.disabled_toolsets", "terminal, file, web, search…")
            + node("search", "app", "tools.tool_search", "off — инструменты видны сразу")
            + node("cwd", "app", "terminal.cwd", "корень репозитория"),
        )
        + cluster(
            "context",
            "Контекст проекта",
            node("rules", "app", ".hermes.md", "правила: числа только из MCP")
            + node("state", "app", "ONEC_MCP_STATE_DIR", ".mcp_state")
            + node("audit", "app", "ONEC_MCP_AUDIT_FILE", "audit.jsonl")
            + node("alerts", "app", "ONEC_ALERTS_ENABLED", "kill switch"),
        )
        + cluster(
            "runtime",
            "Среда",
            node("venv", "tech", "venv Python 3.12+", "зависимости mcp/pandas")
            + node("proc", "tech", "Процесс MCP", "stdio, дочерний у Hermes")
            + node("surface", "tech", "Tool surface", "12 + 4 инструмента"),
        )
        + edge("mcp_srv", "proc", "запускает", "dashed", "onormal")
        + edge("cwd", "rules", "обнаруживает", "dashed", "onormal")
        + edge("toolsets", "surface", "ограничивает")
        + edge("disabled", "surface", "вычитает")
        + edge("search", "surface", "не откладывает")
        + edge("state", "audit", "задаёт путь", "dashed", "onormal")
        + edge("alerts", "state", "включает/выключает", "dashed", "onormal")
        + edge("venv", "proc", "среда исполнения", "dashed", "onormal")
        + edge("rules", "surface", "политика использования", "dashed", "onormal")
    )
    return wrap(
        "configuration-view",
        "Конфигурационное представление",
        "Какие настройки Hermes формируют поверхность инструментов и поведение агента",
        body,
        rankdir="LR",
    )


# --- 13. Flow: dataset switch (UC-3) ---------------------------------------
def dataset_switch_flow() -> str:
    body = (
        node("user", "person", "Пользователь", "«Загрузи витрину …»")
        + node("agent", "external", "Hermes Agent", "вызывает инструмент", "LLM")
        + node("load", "container", "load_dataset(path)", "MCP tool", "stdio")
        + node("check", "component", "Проверка allowlist", "путь внутри репозитория", shape="diamond")
        + node("read", "container", "Чтение CSV + meta", "exported_at / rows", "pandas")
        + node("set", "component", "Смена витрины", "dataset в процессе")
        + node("audit", "store", "audit: dataset_loaded", "JSONL")
        + node("answer", "component", "describe_dataset", "строки, период, дата данных")
        + node("refuse", "risk", "Отказ", "путь вне репозитория")
        + edge("user", "agent", "1 · запрос")
        + edge("agent", "load", "2 · MCP")
        + edge("load", "check", "3 · путь")
        + edge("check", "read", "4 · разрешён")
        + edge("read", "set", "5 · DataFrame")
        + edge("set", "audit", "6 · событие")
        + edge("set", "answer", "7 · сводка")
        + edge("answer", "agent", "8 · ответ", "dashed")
        + edge("check", "refuse", "4′ · запрещён", "dashed")
        + edge("refuse", "agent", "5′ · отказ", "dashed")
    )
    return wrap(
        "dataset-switch-flow",
        "Сценарий UC-3: смена витрины",
        "Загрузка новой выгрузки с проверкой пути, метаданных и аудитом",
        body,
        rankdir="LR",
    )


DIAGRAMS = {
    "01-business-architecture": business_layer,
    "02-system-context": c4_context,
    "03-system-containers": c4_container,
    "04-mcp-components": c4_component,
    "05-adhoc-query-flow": flow_adhoc,
    "06-refusal-flow": flow_refusal,
    "07-returns-alert-flow": flow_alert,
    "08-data-model": data_model,
    "09-deployment-view": deployment,
    "10-trust-zones": security_zones,
    "11-archimate-layers": archimate_layers,
    "12-configuration-view": configuration_view,
    "13-dataset-switch-flow": dataset_switch_flow,
}


def find_dot() -> str:
    found = shutil.which("dot")
    if found:
        return found
    candidates = [
        Path(r"C:\Program Files\Graphviz\bin\dot.exe"),
        Path(r"C:\Program Files (x86)\Graphviz\bin\dot.exe"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise SystemExit("Graphviz dot не найден: установите Graphviz и повторите запуск")


def main() -> int:
    dot = find_dot()
    SRC.mkdir(parents=True, exist_ok=True)
    for name, builder in DIAGRAMS.items():
        dot_src = builder()
        src_path = SRC / f"{name}.dot"
        src_path.write_text(dot_src, encoding="utf-8")
        for fmt, extra in (("svg", []), ("png", ["-Gdpi=200"])):
            target = OUT / f"{name}.{fmt}"
            cmd = [dot, f"-T{fmt}", *extra, str(src_path), "-o", str(target)]
            subprocess.run(cmd, check=True, capture_output=True)
        print(f"{name}: .dot/.svg/.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
