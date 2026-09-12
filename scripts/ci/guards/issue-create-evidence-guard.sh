#!/usr/bin/env bash
# Проверка на входе в scripts/gh/issue-create (#570, второй слой поверх
# #566): улика дефекта в ТЕЛЕ новой issue (путь файла/номер прогона
# Actions/ссылка #N/дословная цитата из блока ```) сверяется с ОТКРЫТЫМИ И
# ЗАКРЫТЫМИ задачами пула — отклоняется ДО вызова `gh issue create`, тот же
# газ `--confirm-not-duplicate`. Регистрация данными (каталог гвардий #749),
# не рукописный шаг .github/workflows/repo-ci.yml.
set -euo pipefail
bash scripts/gh/test/issue-create-evidence-guard.test.sh
