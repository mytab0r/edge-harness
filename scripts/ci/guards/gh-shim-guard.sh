#!/usr/bin/env bash
# Мигрировано из .github/workflows/repo-ci.yml в каталог гвардий (#749):
# рукописный шаг «Smoke gh-шим — gh pr create физически недостижим в обход
# двери (#594)» был добавлен независимо слитым PR ПОСЛЕ заморозки ALLOWLIST
# scripts/lib/ci_guard_registration_guard.py для PR #771 — перенос, не
# поднятие потолка.
#
# PATH-шим gh в среде автономного воркера (#594): живой инцидент — PR #586
# открыт голым `gh pr create` с «Closes #575» изнутри сессии DSH, инструкция
# промпта агента не удержала (транспорт не перехватывает тул-колл агента
# изнутри DSH — openspec/changes/input-schema-not-output-parsing/design.md:
# 205-217). Шим встаёт в PATH раньше настоящего gh (scripts/lib/gh_shim.sh,
# вызывается из scripts/worker/task.sh) — агент физически не может
# достучаться до `gh pr create` в обход scripts/git/pr-create, а сама дверь
# под шимом работает: она резолвит настоящий gh через GH_SHIM_REAL_GH, минуя
# PATH. Мутации, которыми доказан smoke: (1) отключи блок отказа в
# scripts/gh-shim/gh — красный (случай 1); (2) выкини из
# scripts/worker/task.sh source gh_shim.sh, вызов gh_shim_install или
# `|| die` при нём — красный (случай 7, source-гвардия проводки); (3) верни
# в scripts/git/pr-create голый `exec gh pr create` — красный (случай 5a:
# дверь под шимом обязана доходить до настоящего gh, иначе петля «шим →
# дверь → шим»). Это НЕ замена scripts/orchestra/contract_check.py —
# постфактумная проверка остаётся (defense-in-depth: вход и выход оба стоят).
set -euo pipefail
bash scripts/gh-shim/test/gh-shim.test.sh
