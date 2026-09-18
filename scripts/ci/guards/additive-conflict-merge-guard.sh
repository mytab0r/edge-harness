#!/usr/bin/env bash
# Тесты механического сведения аддитивных конфликтов (issue #1032,
# scripts/orchestra/additive_conflict_merge.py): класс «обе стороны
# независимо дописали новый элемент в одну точку общего реестра» сводится
# механически (ast.parse каждой стороны + отсутствие общих имён/ключей,
# верификация ast.parse полного файла + pytest соседей ДО git rebase
# --continue), любое сомнение — консервативный отказ всего PR.
# Подключено скриптом каталога гвардий (#749), не рукописным шагом
# repo-ci.yml: orphan-test-guard («новый test-файл обязан красить CI»)
# + ci-guard-registration (новые шаги в repo-ci.yml запрещены — каталог,
# не шаг).
set -euo pipefail
pip install --quiet pytest pyyaml
python -m pytest scripts/orchestra/test_additive_conflict_merge.py -q
