#!/usr/bin/env bash
# Гвардия проводки проверки свежести worker-configuration.d.ts (issue #1419).
#
# Класс: генерат, свежесть которого проверяется только ПОСЛЕ слияния. Живой
# случай — bump wrangler 4.129.1 → 4.131.2 (37f08c3f, PR #1357) привёз runtime
# types workerd@1.20260911.1, закоммиченный генерат остался от
# workerd@1.20260831.1. Единственная проверка жила в `deploy-worker.yml`: PR
# слился зелёным, а деплой морды падал на ней каждый прогон с 2026-09-13 по
# 2026-09-21 — девять суток прод не обновлялся, и увидеть это можно было
# только вручную, открыв Actions.
#
# Сторожится ПРОВОДКА, не тело. Тело (`cf-worker/scripts/check-types-fresh.mjs`)
# исполняется по-настоящему на каждом прогоне обоих workflow — настоящий
# `wrangler types`, настоящий `git diff`; второй стенд под него был бы
# пересказом внешнего инструмента (AGENTS.md, «Заглушка внешнего инструмента —
# это пересказ»). Удалить можно именно ШАГ — и тогда верное тело просто
# перестанет запускаться, ровно как в #1419.
#
# Доказано мутацией — ИСПОЛНЕНО, не пересказано. База: «4 passed».
#   1) убрать шаг «Типы сгенерированы из текущего wrangler?» из
#      .github/workflows/worker-ci.yml (вернуть ворота на PR в дофиксовое
#      состояние) — «1 failed, 3 passed», краснеет
#      test_both_workflows_run_the_shared_check с текстом
#      «worker-ci.yml: не зовёт `npm run types:check`».
#   2) вернуть тело проверки обратно в YAML второй копией (строка
#      `npx wrangler types` в deploy-worker.yml вместо `npm run types:check`) —
#      «2 failed, 2 passed», краснеют test_both_workflows_run_the_shared_check
#      и test_no_workflow_reinlines_the_check_body: расхождение двух копий
#      фильтра — это и есть отложенный рецидив.
#
# Регистрируется каталогом (#749), не рукописным шагом repo-ci.yml.
set -euo pipefail
pip install --quiet pytest
python -m pytest scripts/lib/test_worker_types_guard.py -q
