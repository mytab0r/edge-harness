#!/usr/bin/env bash
# Класс #507: plugins-src/*/package.json объявляет @deepseek-ai/<pkg> на
# версии, которая разошлась с тем, что реально пиннует dsh-edge/upstream.json
# (apps/dsh-edge/standalone/package.json апстрима на том же коммите). Живой
# случай — issue #806: @deepseek-ai/dsh-settings 0.1.1-rc.2 в
# plugins-src/provider-registry/package.json против реальных 0.1.2-rc.1,
# `installSettingsSection` снята из экспортов между этими версиями — деплой
# падал SyntaxError. Логика и мутация-пруф —
# scripts/lib/plugin_upstream_version_drift.py /
# scripts/lib/test_plugin_upstream_version_drift.py.
#
# Безусловная (не диффовая) проверка — в отличие от
# plugin-manager-roster-guard.sh: этот класс закрыт целиком этим же PR (все
# известные на 2026-09-12 расхождения plugins-src/*/package.json устранены),
# поэтому гвардия стартует зелёной и остаётся объявленным инвариантом, а не
# новым тормозом (AGENTS.md, «Тормоз без газа не принимается»).
set -euo pipefail
pip install --quiet pytest
python -m pytest scripts/lib/test_plugin_upstream_version_drift.py -q
python scripts/lib/plugin_upstream_version_drift.py
