#!/usr/bin/env bash
# Мигрировано из .github/workflows/repo-ci.yml в каталог гвардий (#749):
# шаг «Тесты эскалации автооткота deploy-worker (#614)» был дописан
# рукописно PR'ом #617 ПОСЛЕ заморозки ALLOWLIST
# scripts/lib/ci_guard_registration_guard.py — перенос, не поднятие потолка
# (тот же перенос, что console-utf8-guard.sh после точно такой же ошибки).
#
# Класс «красная канарейка UI на проде после успешного деплоя молчала»
# (#614): текст трёх исходов эскалации автооткота (откат не подтверждён /
# откат прошёл, но повторная канарейка красная — худший случай / откат
# прошёл и повторная канарейка зелёная), проводка env↔main() по реальному
# YAML workflow, гвардия условия отката и гвардия сериализации прод-деплоев.
set -euo pipefail
pip install --quiet pytest pyyaml
python -m pytest scripts/orchestra/test_deploy_worker_rollback_alert.py -q
