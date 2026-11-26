"""Экспортёр Notion: свойства, санитизация, 429-backoff и пометка выгрузки.

Тесты не ходят в сеть и не требуют ``notion-client``: клиент подменяется
записывающей заглушкой, а пакет импортируется только внутри
:func:`notion_export.make_client`.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from onec_analyst import notion_export
from onec_analyst.session_log import (
    SessionLog,
    SessionRecord,
    mark_exported,
    pending_records,
)

REQUIRED_PROPERTIES = {
    "Название",
    "Дата",
    "Провайдер",
    "Модель",
    "Статус",
    "Verdict",
    "Длительность",
    "Токены",
    "Degraded",
    "Ответ модели",
    "ID сессии",
}


class FakeRateLimited(Exception):
    """Ошибка с полями статуса/заголовков, как у notion_client."""

    def __init__(self, status: int, headers: dict[str, str] | None = None) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.headers = headers or {}


class FakeNotion:
    """Записывающая заглушка клиента Notion (без сети)."""

    def __init__(
        self,
        *,
        fail_pages: bool = False,
        ds_list: list[dict] | None = None,
        retrieve_error: Exception | None = None,
        backlog_titles: list[str] | None = None,
        ds_pages: list[dict] | None = None,
        existing_properties: dict | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail_pages = fail_pages
        self.page_counter = 0
        self.ds_list = [{"id": "ds-1"}] if ds_list is None else ds_list
        self.retrieve_error = retrieve_error
        self.backlog_titles = list(backlog_titles or [])
        self.ds_pages = ds_pages
        self.existing_properties = existing_properties
        self.file_uploads = SimpleNamespace(
            create=self._upload_create,
            send=self._upload_send,
            complete=self._upload_complete,
        )
        self.pages = SimpleNamespace(
            create=self._page_create, update=self._page_update
        )
        self.databases = SimpleNamespace(
            create=self._database_create, retrieve=self._database_retrieve
        )
        self.data_sources = SimpleNamespace(
            query=self._query, retrieve=self._retrieve, update=self._update_data_source
        )

    def _query(self, **kwargs):
        self._record("data_sources.query", kwargs)
        if self.ds_pages is not None:
            return {"results": self.ds_pages, "has_more": False, "next_cursor": None}
        return self.query_backlog(self.backlog_titles)

    def _retrieve(self, **kwargs):
        self._record("data_sources.retrieve", kwargs)
        properties = self.existing_properties
        return {"properties": properties or {"Название": {"title": {}}}}

    def _update_data_source(self, **kwargs):
        self._record("data_sources.update", kwargs)
        return {}

    def _record(self, name: str, payload: dict) -> None:
        self.calls.append((name, payload))

    def _upload_create(self, **kwargs):
        self._record("file_uploads.create", kwargs)
        return {"id": "upload-1"}

    def _upload_send(self, **kwargs):
        self._record("file_uploads.send", {"id": kwargs.get("file_upload_id")})
        return {"id": "upload-1"}

    def _upload_complete(self, **kwargs):
        self._record("file_uploads.complete", {"id": kwargs.get("file_upload_id")})
        return {"id": "upload-1"}

    def _page_create(self, **kwargs):
        if self.fail_pages:
            raise RuntimeError("boom")
        self._record("pages.create", kwargs)
        self.page_counter += 1
        return {"id": f"page-{self.page_counter}"}

    def _page_update(self, **kwargs):
        self._record("pages.update", kwargs)
        return {"id": kwargs.get("page_id")}

    def _database_create(self, **kwargs):
        self._record("databases.create", kwargs)
        return {"id": "db-created", "data_sources": [{"id": "ds-created"}]}

    def _database_retrieve(self, **kwargs):
        if self.retrieve_error is not None:
            raise self.retrieve_error
        return {"id": kwargs.get("database_id"), "data_sources": self.ds_list}

    def query_backlog(self, titles: list[str]) -> dict:
        """Ответ ``data_sources.query``: страницы с заданными названиями."""
        return {
            "results": [
                {
                    "id": f"existing-{index}",
                    "properties": {"Название": {"title": [{"plain_text": title}]}},
                }
                for index, title in enumerate(titles)
            ]
        }


SUMMARY = {
    "text": "За 2024 год: Лидер «P1» — 1 000,00 ₽ (66,67 % от суммы).",
    "intent": "top_bottom_products",
    "values": [
        {"item": "P1", "metric": "revenue", "period": "2024", "value": 1000.0},
    ],
    "periods": ["2024"],
    "entities": ["P1", "P2"],
    "table": {
        "columns": ["rank", "product", "revenue", "share_pct"],
        "rows": [[1, "P1", 1000.0, 66.67], [2, "P2", 500.0, 33.33]],
    },
}


@pytest.fixture()
def seeded(session_db_path: Path) -> Path:
    """Одна невыгруженная сессия с «чувствительным» вопросом и сводкой результата."""
    assert SessionLog(session_db_path).record(
        SessionRecord(
            session_id="sess-1",
            question="Покажи выручку по Иванову, почта ivanov@example.com",
            provider="deepseek",
            model="deepseek-v4-flash",
            dsl={"intent": "total", "params": {"date_from": "2024-01-01"}},
            summary=SUMMARY,
            status="ok",
            verdict=True,
            latency_sec=2.5,
        )
    )
    return session_db_path


def _payload_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


# --------------------------------------------------------------------------- #
# Свойства и санитизация
# --------------------------------------------------------------------------- #
def test_properties_cover_all_required_names(seeded: Path) -> None:
    """Свойства ровно те, что заявлены в ТЗ (таблицы/графика среди них нет)."""
    row = pending_records(seeded)[0]
    properties = notion_export.build_properties(row)

    assert set(properties) == REQUIRED_PROPERTIES - {"Токены"}
    assert properties["Статус"] == {"select": {"name": "ok"}}
    assert properties["Verdict"] == {"checkbox": True}
    assert properties["Degraded"] == {"checkbox": False}
    assert properties["Длительность"] == {"number": 2.5}


def test_model_answer_property_keeps_dsl_json(seeded: Path) -> None:
    """«Ответ модели» — компактный JSON DSL, пригодный для чтения и разбора."""
    row = pending_records(seeded)[0]
    property_value = notion_export.build_properties(row)["Ответ модели"]
    content = property_value["rich_text"][0]["text"]["content"]

    assert json.loads(content) == json.loads(row["dsl"])
    # Компактная сериализация: без пробелов после двоеточий.
    assert '": ' not in content


def test_model_answer_is_absent_without_dsl() -> None:
    """Без DSL свойство не отправляется, а не превращается в пустую строку."""
    assert notion_export.model_answer_json({"dsl": None}) is None
    assert notion_export.model_answer_json({"dsl": ""}) is None
    assert notion_export.model_answer_json({"dsl": "не json"}) is None
    assert "Ответ модели" not in notion_export.build_properties({"ts": "2024-01-01"})


def test_model_answer_drops_ui_suggestions() -> None:
    """Подсказки уточнения — UI-хинты, в свойство «Ответ модели» не попадают."""
    dsl = {
        "intent": "clarification",
        "question": "Уточните, что показать?",
        "suggestions": ["Топ-5 за 2024", "Итог за 2024"],
    }
    payload = notion_export.model_answer_json({"dsl": json.dumps(dsl, ensure_ascii=False)})

    assert payload is not None
    parsed = json.loads(payload)
    assert "suggestions" not in parsed
    assert parsed["question"] == "Уточните, что показать?"


def test_tokens_property_is_omitted_when_unknown(seeded: Path) -> None:
    """Токенов в контракте нет — свойство не отправляется вовсе."""
    row = pending_records(seeded)[0]
    assert "Токены" not in notion_export.build_properties(row)


def test_payload_never_contains_question_text(seeded: Path) -> None:
    """Текст вопроса (с e-mail внутри) не попадает ни в свойства, ни в блоки."""
    row = pending_records(seeded)[0]
    payload = {
        "properties": notion_export.build_properties(row),
        "children": notion_export.build_page_blocks(row, chart_upload_id="u1"),
    }
    text = _payload_text(payload)
    assert "Иванов" not in text
    assert "ivanov@example.com" not in text
    assert "D:/" not in text and "D:\\" not in text
    assert notion_export.PROMPT_PLACEHOLDER in text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("test@example.com", notion_export.PII_PLACEHOLDER),
        ("+7 (999) 123-45-67", notion_export.PII_PLACEHOLDER),
        ("8 999 123-45-67", notion_export.PII_PLACEHOLDER),
        ("1234567890", notion_export.PII_PLACEHOLDER),
        ("2024-11-22T10:00:00+03:00", "2024-11-22T10:00:00+03:00"),
        ("2023-01-15", "2023-01-15"),
    ],
)
def test_sanitizer_masks_pii_but_keeps_dates(raw: str, expected: str) -> None:
    """ПДн заменяются, а даты-метаданные остаются нетронутыми."""
    assert notion_export.sanitize_text(raw) == expected


PRODUCT_NAME = "Мука пшеничная"
PARTY_NAME = "ООО Ромашка"
GROUP_NAME = "Бакалея"
MONEY_TEXT = "150 000,50"
MONEY_RAW = "150000.5"
MONEY_THRESHOLD = "250000"


def _sensitive_summary() -> dict:
    """Сводка с названием, контрагентом и суммой — как у реальной сессии 1С."""
    return {
        "text": (
            f"Лидер «{PRODUCT_NAME}» — {MONEY_TEXT} ₽ (12,50 % от суммы). "
            f"Контрагент {PARTY_NAME}."
        ),
        "intent": "top_bottom_products",
        "values": [
            {
                "item": PRODUCT_NAME,
                "metric": "revenue",
                "period": "2024",
                "value": 150000.5,
            }
        ],
        "periods": ["2024"],
        "entities": [PRODUCT_NAME, PARTY_NAME],
        "table": {
            "columns": ["rank", "product", "counterparty", "revenue", "share_pct"],
            "rows": [[1, PRODUCT_NAME, PARTY_NAME, 150000.5, 12.5]],
        },
    }


def _sensitive_dsl() -> dict:
    """DSL с фильтром-названием, порогом суммы и почтой в тексте."""
    return {
        "intent": "counterparty_dynamics",
        "question": f"почта ivanov@example.com, товар {PRODUCT_NAME}",
        "params": {
            "metric": "revenue",
            "product_filter": PRODUCT_NAME,
            "group_filter": GROUP_NAME,
            "min_base_revenue": 250000,
            "limit": 5,
            "start_date": "2024-01-01",
        },
    }


def _seed_source(path: Path, data_source: str) -> None:
    """Одна сессия с заданной подписью источника."""
    assert SessionLog(path).record(
        SessionRecord(
            session_id="sess-src",
            question="секретный вопрос",
            provider="rules",
            model="rules",
            dsl=_sensitive_dsl(),
            summary=_sensitive_summary(),
            status="ok",
            verdict=True,
            latency_sec=1.25,
            data_source=data_source,
        )
    )


def _page_payload(path: Path, tmp_path: Path) -> str:
    """Текст payload ``pages.create`` — то, что уходит в Notion."""
    client = FakeNotion()
    stats = notion_export.export_pending(
        client=client,
        database_id="db-1",
        db_path=path,
        out_dir=tmp_path,
    )
    assert stats == {"pending": 1, "exported": 1, "failed": 0}
    page = next(payload for name, payload in client.calls if name == "pages.create")
    return _payload_text(page)


def test_1c_source_payload_has_no_names_or_amounts(tmp_path: Path) -> None:
    """Сессия с источником 1С → в payload нет названий и сумм."""
    db_path = tmp_path / "sessions.db"
    _seed_source(db_path, "1C")
    row = pending_records(db_path)[0]
    assert row["data_source"] == "1C"
    assert notion_export._chart_pairs(row) == []

    text = _page_payload(db_path, tmp_path / "out")
    for secret in (PRODUCT_NAME, PARTY_NAME, GROUP_NAME, MONEY_TEXT, MONEY_RAW, MONEY_THRESHOLD):
        assert secret not in text
    assert "150000,50" not in text
    assert "150 000" not in text
    assert notion_export.ENTITY_PLACEHOLDER in text
    assert "<AMOUNT>" not in text
    assert "<SUM>" not in text
    assert "ivanov@example.com" not in text
    assert notion_export.PII_PLACEHOLDER in text
    assert "12,50" in text
    assert "2024-01-01" in text


@pytest.mark.parametrize("data_source", ["", "демо-данные", "sales.xlsx"])
def test_other_sources_keep_entity_names(
    tmp_path: Path, data_source: str
) -> None:
    """Демо, файл и пустой источник отдают названия и суммы как раньше."""
    db_path = tmp_path / "sessions.db"
    _seed_source(db_path, data_source)
    text = _page_payload(db_path, tmp_path / "out")
    assert PRODUCT_NAME in text
    assert PARTY_NAME in text
    assert GROUP_NAME in text
    assert MONEY_TEXT in text
    assert MONEY_THRESHOLD in text
    assert notion_export.ENTITY_PLACEHOLDER not in text
    assert "ivanov@example.com" not in text
    assert notion_export.PII_PLACEHOLDER in text


def test_model_answer_json_sanitizes_filter_text() -> None:
    """DSL с фильтром проходит ``sanitize_text``, даже без источника 1С."""
    dsl = {
        "intent": "total",
        "params": {"product_filter": "ivanov@example.com", "limit": 5},
    }
    rendered = notion_export.model_answer_json(
        {"dsl": json.dumps(dsl, ensure_ascii=False), "data_source": "демо-данные"}
    )
    assert rendered is not None
    assert "ivanov@example.com" not in rendered
    assert notion_export.PII_PLACEHOLDER in rendered
    assert json.loads(rendered)["params"]["limit"] == 5


def test_summary_rows_have_no_entity_names(seeded: Path) -> None:
    """В таблице только метаданные: ни вопроса, ни контрагентов, ни товаров."""
    row = pending_records(seeded)[0]
    labels = [label for label, _ in notion_export.summary_rows(row)]
    values = [value for _, value in notion_export.summary_rows(row)]
    assert "Вопрос" in labels
    assert notion_export.PROMPT_PLACEHOLDER in values
    assert "total" in values
    assert all("Иванов" not in value for value in values)


# --------------------------------------------------------------------------- #
# 429 / exponential backoff
# --------------------------------------------------------------------------- #
def test_retry_after_header_is_primary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Заголовок ``Retry-After`` имеет приоритет над exponent-задержкой."""
    sleeps: list[float] = []
    monkeypatch.setattr(notion_export, "_sleep", sleeps.append)
    attempts = {"n": 0}

    def call() -> str:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise FakeRateLimited(429, {"Retry-After": "2"})
        return "ok"

    assert notion_export.call_with_backoff(call) == "ok"
    assert sleeps == [2.0]


def test_backoff_is_exponential_without_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """Без заголовка задержки растут как 1, 2, 4 секунды."""
    sleeps: list[float] = []
    monkeypatch.setattr(notion_export, "_sleep", sleeps.append)
    attempts = {"n": 0}

    def call() -> str:
        attempts["n"] += 1
        if attempts["n"] < 4:
            raise FakeRateLimited(429)
        return "ok"

    assert notion_export.call_with_backoff(call, max_attempts=5) == "ok"
    assert sleeps == [1.0, 2.0, 4.0]


def test_backoff_gives_up_after_max_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Исчерпание попыток пробрасывает последнюю ошибку, а не глотает её."""
    monkeypatch.setattr(notion_export, "_sleep", lambda _seconds: None)
    calls = {"n": 0}

    def call() -> str:
        calls["n"] += 1
        raise FakeRateLimited(429)

    with pytest.raises(FakeRateLimited):
        notion_export.call_with_backoff(call, max_attempts=3)
    assert calls["n"] == 3


def test_backoff_does_not_retry_other_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """400/401 повторять нельзя: это не лимит, а ошибка конфигурации."""
    sleeps: list[float] = []
    monkeypatch.setattr(notion_export, "_sleep", sleeps.append)

    def call() -> str:
        raise FakeRateLimited(400)

    with pytest.raises(FakeRateLimited):
        notion_export.call_with_backoff(call)
    assert sleeps == []


def test_backoff_survives_retry_after_overflow(monkeypatch: pytest.MonkeyPatch) -> None:
    """Огромный ``Retry-After`` обрезается потолком ``max_delay``."""
    sleeps: list[float] = []
    monkeypatch.setattr(notion_export, "_sleep", sleeps.append)
    attempts = {"n": 0}

    def call() -> str:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise FakeRateLimited(429, {"Retry-After": "3600"})
        return "ok"

    notion_export.call_with_backoff(call, max_delay=30.0)
    assert sleeps == [30.0]


# --------------------------------------------------------------------------- #
# Полный проход экспорта
# --------------------------------------------------------------------------- #
def test_export_creates_page_and_marks_exported(seeded: Path, tmp_path: Path) -> None:
    """Один проход: страница с таблицей и PNG, запись помечена выгруженной."""
    client = FakeNotion()
    stats = notion_export.export_pending(
        client=client,
        database_id="db-1",
        db_path=seeded,
        out_dir=tmp_path,
        chart_builder=lambda rows: b"\x89PNG-fake",
    )

    assert stats == {"pending": 1, "exported": 1, "failed": 0}
    names = [name for name, _ in client.calls]
    # single_part: send финализирует upload, complete вызывать нельзя (400 у Notion).
    assert "file_uploads.send" in names
    assert "file_uploads.complete" not in names
    page_call = next(payload for name, payload in client.calls if name == "pages.create")
    children_types = [block["type"] for block in page_call["children"]]
    assert children_types == [
        "heading_2",
        "paragraph",
        "table",
        "heading_2",
        "table",
        "image",
    ]
    # Полный путь к локальному файлу в Notion не отправляется.
    assert "Локальный экспорт" not in _payload_text(page_call)
    assert page_call["parent"] == {
        "type": "data_source_id",
        "data_source_id": "ds-1",
    }
    assert page_call["properties"]["Статус"] == {"select": {"name": "ok"}}

    # Локальная копия остаётся на диске.
    assert list(tmp_path.glob("session_*.md"))
    assert list(tmp_path.glob("session_*.png"))

    # Отметка выгрузки: повторный проход ничего не находит.
    assert pending_records(seeded) == []
    connection = sqlite3.connect(seeded)
    try:
        exported_at, page_id = connection.execute(
            "SELECT exported_at, notion_page_id FROM sessions"
        ).fetchone()
    finally:
        connection.close()
    assert exported_at and page_id == "page-1"


def test_page_body_contains_result_table(seeded: Path) -> None:
    """В теле страницы есть блок «Результат»: текст сводки и таблица значений."""
    row = pending_records(seeded)[0]
    blocks = notion_export.build_page_blocks(row, chart_upload_id="u1")
    assert blocks[0]["heading_2"]["rich_text"][0]["text"]["content"] == "Результат"
    assert "Лидер" in blocks[1]["paragraph"]["rich_text"][0]["text"]["content"]
    result_table = blocks[2]["table"]
    cells = [
        [
            "".join(part["text"]["content"] for part in cell)
            for cell in block["table_row"]["cells"]
        ]
        for block in result_table["children"]
    ]
    assert cells[0] == ["№", "Товар", "Выручка без НДС, ₽", "Доля, %"]
    assert cells[1] == ["1", "P1", "1 000,00", "66,67"]
    assert cells[2][0] == "2"


def test_result_rows_empty_without_summary() -> None:
    """Без сводки (уточнение/отказ) блок результата не строится."""
    assert notion_export.result_rows({"session_id": "s"}) == ("", [])
    assert notion_export.result_rows({"session_id": "s", "summary": "не json"}) == ("", [])


def test_chart_uses_result_values(seeded: Path) -> None:
    """График для сессии с результатом — валидный PNG (столбики по значениям)."""
    row = pending_records(seeded)[0]
    assert notion_export.build_chart([row]).startswith(b"\x89PNG")


def test_local_markdown_has_result_section(seeded: Path, tmp_path: Path) -> None:
    """Markdown-копия повторяет структуру страницы: результат + метаданные."""
    row = pending_records(seeded)[0]
    markdown_path, _ = notion_export.write_local_export(row, tmp_path, chart_png=b"png")
    text = markdown_path.read_text(encoding="utf-8")
    assert "## Результат" in text
    assert "| 1 | P1 | 1 000,00 | 66,67 |" in text
    assert "## Сессия" in text


def test_no_chart_for_non_ok_session(
    session_db_path: Path, tmp_path: Path
) -> None:
    """У clarify/refuse/error результата нет — картинку не строим и не грузим."""
    assert SessionLog(session_db_path).record(
        SessionRecord(
            session_id="c1",
            chat_id="chat-c",
            turn=1,
            status="clarify",
            verdict=False,
        )
    )
    client = FakeNotion()
    stats = notion_export.export_pending(
        client=client,
        database_id="db-1",
        db_path=session_db_path,
        out_dir=tmp_path,
        chart_builder=lambda rows: b"png",
    )

    assert stats == {"pending": 1, "exported": 1, "failed": 0}
    page_call = next(payload for name, payload in client.calls if name == "pages.create")
    assert [block["type"] for block in page_call["children"]] == ["heading_2", "table"]
    assert not any(name.startswith("file_uploads") for name, _ in client.calls)
    # Локальная копия остаётся, но без PNG.
    assert list(tmp_path.glob("session_*.md"))
    assert not list(tmp_path.glob("session_*.png"))


def test_chart_is_built_for_ok_session(session_db_path: Path, tmp_path: Path) -> None:
    """Успешная сессия по-прежнему получает график."""
    assert SessionLog(session_db_path).record(
        SessionRecord(session_id="o1", chat_id="chat-o", turn=1, status="ok", verdict=True)
    )
    client = FakeNotion()
    notion_export.export_pending(
        client=client,
        database_id="db-1",
        db_path=session_db_path,
        out_dir=tmp_path,
        chart_builder=lambda rows: b"\x89PNG",
    )

    page_call = next(payload for name, payload in client.calls if name == "pages.create")
    assert [block["type"] for block in page_call["children"]][-1] == "image"
    assert list(tmp_path.glob("session_*.png"))


def test_export_dry_run_touches_nothing(seeded: Path) -> None:
    """``--dry-run`` не создаёт страниц и не помечает записи выгруженными."""
    client = FakeNotion()
    stats = notion_export.export_pending(
        client=client,
        database_id="db-1",
        db_path=seeded,
        dry_run=True,
        chart_builder=lambda rows: b"png",
    )

    assert stats == {"pending": 1, "exported": 1, "failed": 0}
    assert client.calls == []
    assert len(pending_records(seeded)) == 1


def test_failed_page_keeps_row_pending(seeded: Path, tmp_path: Path) -> None:
    """Сбой создания страницы не теряет запись: следующий проход её заберёт."""
    client = FakeNotion(fail_pages=True)
    stats = notion_export.export_pending(
        client=client,
        database_id="db-1",
        db_path=seeded,
        out_dir=tmp_path,
        chart_builder=lambda rows: b"png",
    )

    assert stats["failed"] == 1
    assert stats["exported"] == 0
    assert len(pending_records(seeded)) == 1


def test_export_empty_journal_is_noop(session_db_path: Path) -> None:
    """Пустой журнал — штатное завершение без обращений к Notion."""
    client = FakeNotion()
    stats = notion_export.export_pending(
        client=client,
        database_id="db-1",
        db_path=session_db_path,
    )
    assert stats == {"pending": 0, "exported": 0, "failed": 0}
    assert client.calls == []


def test_local_export_markdown_has_no_question(seeded: Path, tmp_path: Path) -> None:
    """Локальная markdown-копия тоже без текста вопроса — её можно переслать."""
    row = pending_records(seeded)[0]
    markdown_path, png_path = notion_export.write_local_export(
        row, tmp_path, chart_png=b"png"
    )
    text = markdown_path.read_text(encoding="utf-8")
    assert notion_export.PROMPT_PLACEHOLDER in text
    assert "ivanov@example.com" not in text
    assert png_path is not None and png_path.exists()


def test_database_schema_has_four_statuses() -> None:
    """Схема создаваемой базы: 8 свойств, статус — ровно 4 значения."""
    schema = notion_export.database_schema()
    assert set(schema) == REQUIRED_PROPERTIES
    options = [option["name"] for option in schema["Статус"]["select"]["options"]]
    assert options == ["ok", "clarify", "refuse", "error"]


def test_create_database_returns_ids_and_initial_schema() -> None:
    """``--create-database`` отправляет схему в ``initial_data_source`` (API 2025-09-03)."""
    client = FakeNotion()
    database_id, data_source_id = notion_export.create_database(
        client, "page-parent", "Сессии"
    )
    assert database_id == "db-created"
    assert data_source_id == "ds-created"
    call = client.calls[-1][1]
    assert call["parent"] == {"type": "page_id", "page_id": "page-parent"}
    assert set(call["initial_data_source"]["properties"]) == REQUIRED_PROPERTIES


# --------------------------------------------------------------------------- #
# Свойство «Модель» (TASK-17) и дозаполнение истории
# --------------------------------------------------------------------------- #
def test_model_property_is_next_to_provider_in_schema() -> None:
    """«Модель» — select сразу после «Провайдер»; «ID сессии» — служебный rich_text."""
    schema = notion_export.database_schema()
    names = list(schema)

    assert names.index("Модель") == names.index("Провайдер") + 1
    assert schema["Модель"] == {"select": {}}
    assert schema["ID сессии"] == {"rich_text": {}}


def test_model_property_uses_journal_model(seeded: Path) -> None:
    """Значение берётся из ``model`` журнала, а не из настроек окружения."""
    row = pending_records(seeded)[0]
    assert notion_export.build_properties(row)["Модель"] == {
        "select": {"name": "deepseek-v4-flash"}
    }


def test_model_property_marks_rules_mode(session_db_path: Path) -> None:
    """У сессии режима правил модели нет — в свойстве ``rules``, а не пусто."""
    assert SessionLog(session_db_path).record(
        SessionRecord(session_id="r1", provider="none", model="", status="ok")
    )
    row = pending_records(session_db_path)[0]
    assert notion_export.build_properties(row)["Модель"] == {"select": {"name": "rules"}}


def test_model_property_absent_when_model_unknown(session_db_path: Path) -> None:
    """Настоящий провайдер без записанной модели — значение не выдумывается."""
    assert SessionLog(session_db_path).record(
        SessionRecord(session_id="u1", provider="deepseek", model=None, status="ok")
    )
    row = pending_records(session_db_path)[0]
    assert "Модель" not in notion_export.build_properties(row)


def test_session_id_property_is_exported(seeded: Path) -> None:
    """«ID сессии» уходит на страницу: по нему потом сопоставляется история."""
    row = pending_records(seeded)[0]
    value = notion_export.build_properties(row)["ID сессии"]
    assert value["rich_text"][0]["text"]["content"] == "sess-1"


def test_session_id_with_digit_run_is_not_masked(session_db_path: Path) -> None:
    """12 цифр подряд внутри uuid — не повод подменить ключ на ``<PII>``."""
    session_id = "111122223333444455556666777788889999"
    assert SessionLog(session_db_path).record(
        SessionRecord(
            session_id=session_id,
            provider="deepseek",
            model="deepseek-v4-flash",
            status="ok",
        )
    )
    row = pending_records(session_db_path)[0]
    value = notion_export.build_properties(row)["ID сессии"]

    assert value["rich_text"][0]["text"]["content"] == session_id


def test_ensure_properties_adds_model_and_session_id() -> None:
    """--ensure-properties доливает только отсутствующие свойства."""
    existing = {
        name: schema
        for name, schema in notion_export.database_schema().items()
        if name not in {"Модель", "ID сессии"}
    }
    client = FakeNotion(existing_properties=existing)

    added = notion_export.ensure_properties(client, "db-1")

    assert added == ["ID сессии", "Модель"]
    update = next(payload for name, payload in client.calls if name == "data_sources.update")
    assert set(update["properties"]) == {"Модель", "ID сессии"}


def _exported_row(
    db: Path,
    *,
    ts: str = "2026-11-22T10:00:05+03:00",
    session_id: str = "sess-42",
    provider: str = "deepseek",
    model: str = "deepseek-v4-flash",
    status: str = "ok",
    latency_sec: float = 2.5,
) -> dict:
    """Записать сессию и отметить её выгруженной (как после прошлого экспорта)."""
    assert SessionLog(db).record(
        SessionRecord(
            session_id=session_id,
            ts=ts,
            provider=provider,
            model=model,
            status=status,
            latency_sec=latency_sec,
        )
    )
    row = pending_records(db)[0]
    mark_exported(db, int(row["id"]), f"page-{session_id}")
    return row


def _notion_page(
    *,
    page_id: str,
    ts: str = "2026-11-22T10:00:00.000+03:00",
    provider: str = "deepseek",
    status: str = "ok",
    session_id: str | None = None,
    latency: float | None = 2.5,
) -> dict:
    """Страница, какой её отдаёт ``data_sources.query`` (свойства, без блоков).

    ``Дата`` намеренно в формате Notion: время до минуты и миллисекунды.
    """
    properties: dict = {
        "Название": {"title": [{"plain_text": f"Сессия {ts[:10]} · {status}"}]},
        "Дата": {"date": {"start": ts}},
        "Провайдер": {"select": {"name": provider}},
        "Статус": {"select": {"name": status}},
        "Длительность": {"number": latency},
    }
    if session_id is not None:
        properties["ID сессии"] = {"rich_text": [{"plain_text": session_id}]}
    return {"id": page_id, "properties": properties}


def test_backfill_history_fills_model_and_session_id(session_db_path: Path) -> None:
    """Историческая страница получает «Модель» и «ID сессии» по журналу."""
    _exported_row(session_db_path)
    client = FakeNotion(
        ds_pages=[_notion_page(page_id="page-old", ts="2026-11-22T10:00:00+03:00")]
    )

    stats = notion_export.backfill_history(
        client=client, database_id="db-1", db_path=session_db_path
    )

    assert stats == {"pages": 1, "updated": 1, "skipped": 0, "unmatched": 0}
    update = next(payload for name, payload in client.calls if name == "pages.update")
    assert update["page_id"] == "page-old"
    assert update["properties"]["Модель"] == {"select": {"name": "deepseek-v4-flash"}}
    assert update["properties"]["ID сессии"]["rich_text"][0]["text"]["content"] == "sess-42"


def test_backfill_history_skips_page_with_session_id(session_db_path: Path) -> None:
    """Страница с уже заполненным «ID сессии» не обновляется (идемпотентность)."""
    _exported_row(session_db_path)
    client = FakeNotion(
        ds_pages=[
            _notion_page(
                page_id="page-done",
                ts="2026-11-22T10:00:00+03:00",
                session_id="sess-42",
            )
        ]
    )

    stats = notion_export.backfill_history(
        client=client, database_id="db-1", db_path=session_db_path
    )

    assert stats == {"pages": 1, "updated": 0, "skipped": 1, "unmatched": 0}
    assert not any(name == "pages.update" for name, _ in client.calls)


def test_backfill_history_leaves_unmatched_page_alone(session_db_path: Path) -> None:
    """Страница без пары в журнале только считается: её не трогают."""
    _exported_row(session_db_path)
    client = FakeNotion(
        ds_pages=[_notion_page(page_id="page-x", ts="2025-01-01T00:00:00+03:00")]
    )

    stats = notion_export.backfill_history(
        client=client, database_id="db-1", db_path=session_db_path
    )

    assert stats == {"pages": 1, "updated": 0, "skipped": 0, "unmatched": 1}
    assert not any(name == "pages.update" for name, _ in client.calls)


def test_backfill_history_disambiguates_same_minute_by_latency(
    session_db_path: Path,
) -> None:
    """Две сессии в одну минуту: пара выбирается по «Длительности»."""
    _exported_row(
        session_db_path,
        ts="2026-11-22T10:00:05+03:00",
        session_id="sess-a",
        latency_sec=1.25,
    )
    _exported_row(
        session_db_path,
        ts="2026-11-22T10:00:40+03:00",
        session_id="sess-b",
        latency_sec=7.5,
    )
    client = FakeNotion(
        ds_pages=[
            _notion_page(
                page_id="page-b",
                ts="2026-11-22T10:00:00.000+03:00",
                latency=7.5,
            )
        ]
    )

    stats = notion_export.backfill_history(
        client=client, database_id="db-1", db_path=session_db_path
    )

    assert stats["updated"] == 1
    update = next(payload for name, payload in client.calls if name == "pages.update")
    assert update["properties"]["ID сессии"]["rich_text"][0]["text"]["content"] == "sess-b"


def test_backfill_history_does_not_borrow_other_provider_row(
    session_db_path: Path,
) -> None:
    """Провайдер страницы не совпал с журналом — страница остаётся без пары."""
    _exported_row(session_db_path, provider="deepseek", session_id="sess-ds")
    client = FakeNotion(
        ds_pages=[
            _notion_page(
                page_id="page-none",
                ts="2026-11-22T10:00:00.000+03:00",
                provider="none",
            )
        ]
    )

    stats = notion_export.backfill_history(
        client=client, database_id="db-1", db_path=session_db_path
    )

    assert stats == {"pages": 1, "updated": 0, "skipped": 0, "unmatched": 1}
    assert not any(name == "pages.update" for name, _ in client.calls)


def test_minute_key_ignores_seconds_and_tz_offset() -> None:
    """Ключ момента: секунды и формат Notion (``.000``) не мешают сопоставлению."""
    key = notion_export._minute_key

    assert key("2026-11-22T10:00:05+03:00") == key("2026-11-22T10:00:00.000+03:00")
    assert key("2026-11-22T07:00:05+00:00") == key("2026-11-22T10:00:05+03:00")
    assert key("2026-11-22T10:01:00+03:00") != key("2026-11-22T10:00:00+03:00")
    assert key("") == "" and key(None) == ""
    assert key("не дата") == "не дата"


def test_backfill_history_dry_run_writes_nothing(session_db_path: Path) -> None:
    """``--dry-run`` считает план и не вызывает ``pages.update``."""
    _exported_row(session_db_path)
    client = FakeNotion(
        ds_pages=[_notion_page(page_id="page-old", ts="2026-11-22T10:00:00+03:00")]
    )

    stats = notion_export.backfill_history(
        client=client, database_id="db-1", db_path=session_db_path, dry_run=True
    )

    assert stats["updated"] == 1
    assert not any(name == "pages.update" for name, _ in client.calls)


def test_backfill_history_requires_data_source() -> None:
    """База старого формата: перечислять страницы нечем — понятная ошибка."""
    client = FakeNotion(ds_list=[])
    with pytest.raises(ValueError, match="data source"):
        notion_export.backfill_history(client=client, database_id="legacy-db")


def test_legacy_database_without_data_sources_uses_database_parent() -> None:
    """База старого формата (без ``data_sources``) — родитель ``database_id``."""
    client = FakeNotion(ds_list=[])
    parent = notion_export.resolve_parent(client, "legacy-db")
    assert parent == {"type": "database_id", "database_id": "legacy-db"}


def test_data_source_id_is_accepted_directly() -> None:
    """На вход можно дать уже id data source: 404 при чтении базы — не ошибка."""
    client = FakeNotion(retrieve_error=FakeRateLimited(404))
    parent = notion_export.resolve_parent(client, "ds-only")
    assert parent == {"type": "data_source_id", "data_source_id": "ds-only"}


def test_cli_requires_api_key(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Без ``NOTION_API_KEY`` — код возврата 2 и понятная ошибка."""
    monkeypatch.delenv("NOTION_API_KEY", raising=False)
    monkeypatch.delenv("NOTION_DATABASE_ID", raising=False)
    caplog.set_level("ERROR")
    assert notion_export.main([]) == 2
    assert "NOTION_API_KEY" in caplog.text


def test_cli_requires_database_id(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Ключ есть, id базы нет — подсказка про ``--create-database``."""
    monkeypatch.setenv("NOTION_API_KEY", "secret")
    monkeypatch.delenv("NOTION_DATABASE_ID", raising=False)
    monkeypatch.setattr(notion_export, "make_client", lambda token: FakeNotion())
    caplog.set_level("ERROR")
    assert notion_export.main([]) == 2
    assert "NOTION_DATABASE_ID" in caplog.text


def test_chart_is_png() -> None:
    """Реальный график — валидный PNG (matplotlib, backend Agg)."""
    png = notion_export.build_chart(
        [{"latency_sec": 1.5, "status": "ok"}, {"latency_sec": 0.5, "status": "clarify"}]
    )
    assert png.startswith(b"\x89PNG")


def test_export_by_chat_exports_only_active_dialog(
    session_db_path: Path, tmp_path: Path
) -> None:
    """Кнопка «Выгрузить» отправляет только сессии активного диалога."""
    log = SessionLog(session_db_path)
    assert log.record(SessionRecord(session_id="a1", chat_id="chat-a", turn=1, status="ok"))
    assert log.record(SessionRecord(session_id="b1", chat_id="chat-b", turn=1, status="ok"))

    client = FakeNotion()
    stats = notion_export.export_pending(
        client=client,
        database_id="db-1",
        db_path=session_db_path,
        out_dir=tmp_path,
        chat_id="chat-a",
        chart_builder=lambda rows: b"png",
    )

    assert stats == {"pending": 1, "exported": 1, "failed": 0}
    assert len([name for name, _ in client.calls if name == "pages.create"]) == 1
    assert pending_records(session_db_path, chat_id="chat-a") == []
    assert len(pending_records(session_db_path, chat_id="chat-b")) == 1


def test_repeated_export_by_chat_creates_no_duplicates(
    session_db_path: Path, tmp_path: Path
) -> None:
    """Повторное нажатие не создаёт страницу второй раз (дублей нет)."""
    assert SessionLog(session_db_path).record(
        SessionRecord(session_id="a1", chat_id="chat-a", turn=1, status="ok")
    )
    client = FakeNotion()
    kwargs = {
        "client": client,
        "database_id": "db-1",
        "db_path": session_db_path,
        "out_dir": tmp_path,
        "chat_id": "chat-a",
        "chart_builder": lambda rows: b"png",
    }
    first = notion_export.export_pending(**kwargs)
    second = notion_export.export_pending(**kwargs)

    assert first["exported"] == 1
    assert second == {"pending": 0, "exported": 0, "failed": 0}
    assert len([name for name, _ in client.calls if name == "pages.create"]) == 1


def test_export_chat_requires_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Без ключей ``export_chat`` явно говорит, чего не хватает."""
    monkeypatch.delenv("NOTION_API_KEY", raising=False)
    monkeypatch.delenv("NOTION_DATABASE_ID", raising=False)
    with pytest.raises(notion_export.ExportNotConfigured):
        notion_export.export_chat("chat-a")


def test_export_chat_passes_chat_id_to_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``export_chat`` выгружает именно переданный диалог."""
    seen: dict = {}

    def fake_pending(**kwargs):
        seen.update(kwargs)
        return {"pending": 0, "exported": 0, "failed": 0}

    monkeypatch.setattr(notion_export, "export_pending", fake_pending)
    monkeypatch.setattr(notion_export, "make_client", lambda token: object())
    stats = notion_export.export_chat(
        "chat-42", token="secret", database_id="db-1"
    )
    assert stats["pending"] == 0
    assert seen["chat_id"] == "chat-42"

# --------------------------------------------------------------------------- #
# Беклог в Notion: отдельная база задач
# --------------------------------------------------------------------------- #
def test_backlog_schema_has_statuses_and_types() -> None:
    """Схема беклога: статусы, приоритеты, типы и поля описания."""
    schema = notion_export.backlog_schema()

    assert set(schema) == {
        "Название",
        "ID",
        "Статус",
        "Приоритет",
        "Тип",
        "Спринт",
        "Дата",
        "Описание",
        "Критерий готовности",
        "Ссылка",
        "Источник",
    }
    assert schema["ID"] == {"unique_id": {"prefix": "TASK"}}
    assert schema["Ссылка"] == {"url": {}}
    assert schema["Спринт"]["number"]["format"] == "number"
    assert [option["name"] for option in schema["Статус"]["select"]["options"]] == [
        "Идея",
        "Беклог",
        "В работе",
        "Готово",
    ]
    assert [option["name"] for option in schema["Тип"]["select"]["options"]] == [
        "Функция",
        "Дефект",
        "Техдолг",
        "Исследование",
    ]


def test_create_backlog_database_returns_ids() -> None:
    """Создание базы беклога возвращает id базы и data source, схема уходит в initial."""
    client = FakeNotion()
    database_id, data_source_id = notion_export.create_backlog_database(
        client, "page-parent", "Беклог"
    )

    assert (database_id, data_source_id) == ("db-created", "ds-created")
    call = client.calls[-1][1]
    assert call["parent"] == {"type": "page_id", "page_id": "page-parent"}
    assert set(call["initial_data_source"]["properties"]) == set(
        notion_export.backlog_schema()
    )


def test_add_backlog_item_sets_properties() -> None:
    """Карточка задачи: статус, приоритет, тип, дата, спринт и критерий готовности."""
    client = FakeNotion()
    page_id = notion_export.add_backlog_item(
        client,
        "db-1",
        title="Идея",
        description="Что сделать",
        status="Беклог",
        priority="Высокий",
        kind="Функция",
        source="Обсуждение",
        day="2026-11-22",
        sprint=3,
        acceptance="Тест зелёный, сценарий проверен руками",
        url="https://example.com/task",
    )

    assert page_id == "page-1"
    call = next(payload for name, payload in client.calls if name == "pages.create")
    assert call["parent"] == {"type": "data_source_id", "data_source_id": "ds-1"}
    assert call["properties"]["Название"]["title"][0]["text"]["content"] == "Идея"
    assert call["properties"]["Статус"] == {"select": {"name": "Беклог"}}
    assert call["properties"]["Приоритет"] == {"select": {"name": "Высокий"}}
    assert call["properties"]["Дата"] == {"date": {"start": "2026-11-22"}}
    assert call["properties"]["Спринт"] == {"number": 3}
    assert call["properties"]["Ссылка"] == {"url": "https://example.com/task"}
    assert "Тест зелёный" in str(call["properties"]["Критерий готовности"])
    assert "Что сделать" in str(call["properties"]["Описание"])


def test_add_backlog_item_rejects_duplicate() -> None:
    """Дубль по названию не создаётся: понятная ошибка со ссылкой на карточку."""
    client = FakeNotion(backlog_titles=["Детерминированный режим: вопрос целиком как JSON-DSL"])

    with pytest.raises(notion_export.BacklogItemExists) as error:
        notion_export.add_backlog_item(
            client,
            "db-1",
            title="  детерминированный   РЕЖИМ: вопрос целиком как JSON-DSL ",
        )

    assert error.value.page_id == "existing-0"
    assert not any(name == "pages.create" for name, _ in client.calls)


def test_add_backlog_item_allows_duplicate_explicitly() -> None:
    """Осознанно разрешённый дубль создаётся (переоткрытая задача)."""
    client = FakeNotion(backlog_titles=["Идея"])
    page_id = notion_export.add_backlog_item(
        client, "db-1", title="Идея", allow_duplicate=True
    )

    assert page_id == "page-1"
    assert not any(name == "data_sources.query" for name, _ in client.calls)


def test_normalize_title_is_case_and_yo_insensitive() -> None:
    """Сравниваем названия без регистра, лишних пробелов и различия ё/е."""
    normalize = notion_export._normalize_title
    assert normalize("  Вопрос   про Ёжика ") == normalize("вопрос про ежика")
    assert normalize("") == ""
    assert normalize(None) == ""


def test_cli_create_backlog_requires_parent(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    """``--create-backlog`` без страницы-родителя — понятная ошибка и код 2."""
    monkeypatch.setenv("NOTION_API_KEY", "secret")
    monkeypatch.setattr(notion_export, "make_client", lambda token: FakeNotion())
    caplog.set_level("ERROR")
    assert notion_export.main(["--create-backlog"]) == 2
    assert "parent-page" in caplog.text

