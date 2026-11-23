# Политика публичных данных

Принцип (`docs/ARCHITECTURE-SOLUTION.md`): публичные артефакты не содержат
данных заказчика и бизнес-цифр. Проверяется в CI:
`tests/test_public_figures.py`.

## Что публикуется

- синтетическая витрина `sample_data.csv` (600 строк, 2023-01-01…2024-12-28);
- любые числа, воспроизводимые из неё (итоги, топы, дельты, возвраты);
- параметры алгоритмов из `tests/fixtures/public_figures_allowlist.json` —
  с обоснованием.

## Что не публикуется

- суммы, объёмы, диапазоны дат и номенклатура непубличной базы 1С;
- учётные данные и пути к базе, имена рабочих каталогов;
- реальные выгрузки (`1c_export/`), SQLite-журналы (`db/`), сырые ответы
  моделей.

## Как воспроизвести эталонные цифры

```powershell
docker compose up -d --build --wait   # → http://localhost:8501
python -m pytest tests/test_public_figures.py -q
```

## Проверка перед пушем

```powershell
python tools/sanitize_public_artifacts.py --check
python -m pytest tests/test_public_figures.py tests/test_no_local_paths.py -q
```

Исторические записи CHANGELOG/DECISIONS отредактированы: абсолютные значения
заменены качественными формулировками, примеры переведены на синтетическую
номенклатуру. Технические выводы и решения не изменены.
