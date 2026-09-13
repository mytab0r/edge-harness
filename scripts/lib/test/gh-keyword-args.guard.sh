#!/usr/bin/env bash
# Носитель тела гвардии «оркестрация без keyword-аргументов gh()» (#1069,
# ревью PR #1117, находка 1 — шаг мигрирован из repo-ci.yml в каталог этим
# PR; тело run: перенесено дословно). Файл в директории `test/` канарейка
# осиротевших тестов видит: удаление обёртки
# scripts/ci/guards/gh-keyword-args-guard.sh красит, а не молчит.
#
# Гвардия класса #124: keyword body= внутри gh-обёртки (gh(*args)) не
# принимается и роняет ВЕСЬ прогон оркестратора — 2026-08-31 мержи стояли,
# пока краш не заметил человек. Правильная форма: значение "-f", "body=…".
# scripts/lib включён с появления claim_task (#121): там свой gh-обёрточный
# скрипт — класс закрывается во всех точках входа, а не только в orchestra.
# scripts/review — с появления gh-обёрток ai_review/file_tasks (#18).
set -euo pipefail
if grep -rnE ',\s*body=' scripts/orchestra/ scripts/lib/ scripts/review/; then
  echo "::error::keyword body= в вызове gh() — роняет прогон оркестратора (класс #124)"
  exit 1
fi
echo "orchestra+lib+review: keyword-аргументов gh() нет"
