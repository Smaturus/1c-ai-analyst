"""Регресс-тест: в ``app.py`` нет «голых» строковых литералов уровня модуля.

Streamlit-magic исполняет выражения верхнего уровня основного скрипта и
рендерит строковые литералы как обычный текст страницы. Из-за этого
docstring-подобные строки после констант («Русские подписи колонок…»,
«Базовая высота графика, px.», «Цвета баров динамики…») появлялись
над заголовком приложения. Все они переведены в ``#``-комментарии.

Docstring самого модуля (первое выражение в файле) — допустимое исключение.
"""

from __future__ import annotations

import ast

from tests.paths import APP_FILE as APP_PATH


def test_app_has_no_bare_module_level_strings() -> None:
    """Любая строка-выражение уровня модуля, кроме docstring модуля, — баг UI."""
    tree = ast.parse(APP_PATH.read_text(encoding="utf-8"))

    offenders = [
        node.lineno
        for index, node in enumerate(tree.body)
        if index > 0
        and isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    ]

    assert offenders == [], (
        "app.py: «голые» строки уровня модуля будут отрисованы Streamlit-magic "
        f"как текст страницы (ожидались #-комментарии): строки {offenders}"
    )


def test_app_module_docstring_is_single_bare_string() -> None:
    """Docstring модуля разрешён ровно один и находится в начале файла."""
    tree = ast.parse(APP_PATH.read_text(encoding="utf-8"))

    string_expressions = [
        node.lineno
        for node in tree.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    ]

    assert len(string_expressions) <= 1, string_expressions
    if string_expressions:
        assert tree.body.index(
            next(
                node
                for node in tree.body
                if isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            )
        ) == 0


def test_progress_notice_is_created_before_answer_container() -> None:
    """Плашка «Разбираю вопрос…» должна стоять выше таблицы результата.

    Streamlit рисует элементы в порядке создания: если ``notice_slot`` создан
    после контейнера ответа, во время расчёта плашка оказывается под уже
    показанной таблицей и уходит за пределы экрана. Поэтому спиннер обязан
    рисоваться внутри ``notice_slot`` (``with notice_slot:``), а сам слот —
    создаваться раньше ``answer_slot``.
    """
    source = APP_PATH.read_text(encoding="utf-8")
    notice = source.index("notice_slot = st.empty()")
    answer = source.index('answer_slot = st.container(key="answer_block")')

    assert notice < answer, (
        "notice_slot создан после answer_slot: индикатор расчёта окажется под "
        "таблицей и не будет виден. Перенесите создание notice_slot выше."
    )
    assert "with notice_slot:" in source, (
        "спиннер рисуется в основном потоке: Streamlit создаст его после "
        "контейнера ответа, и плашка уйдёт под таблицу. Оберните расчёт в "
        "`with notice_slot:` (см. TASK-16)."
    )
