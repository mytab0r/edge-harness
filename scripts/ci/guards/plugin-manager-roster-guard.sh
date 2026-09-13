#!/usr/bin/env bash
# Класс #806: плагин добавлен/изменён в dsh-edge/plugins.json в этом PR, а
# @edge-harness/dsh-plugin-manager (несущий вшитый ПОЛНЫЙ ростер, build.mjs) не
# пересобран — деплой падает на «Ростер манифеста … разошёлся» ПОСТФАКТУМ на
# main, а не на PR (живой прецедент: 9 подряд красных прогонов
# deploy-dsh-edge.yml с 2026-09-08, коммит 0a85ddf5). Логика и мутация-пруф —
# scripts/lib/plugin_manager_roster_guard.py /
# scripts/lib/test_plugin_manager_roster_guard.py.
#
# GH_TOKEN — env шага-перебора «Каталог гвардий … — перебор» в repo-ci.yml
# (не отдельная проводка для этого файла): gh api читает pull_request.base.sha
# манифеста ОДНИМ сетевым вызовом, не полный git-history диапазон.
set -euo pipefail
pip install --quiet pytest
python -m pytest scripts/lib/test_plugin_manager_roster_guard.py -q
python scripts/lib/plugin_manager_roster_guard.py
