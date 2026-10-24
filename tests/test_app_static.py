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
from pathlib import Path

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"


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
