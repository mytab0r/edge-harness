#!/usr/bin/env bash
# Перенесено из .github/workflows/repo-ci.yml транслятором рукописных
# шагов гвардий (issue #897, продолжение #749/#771/#762/#764): исходный
# шаг «Гвардия пакетного менеджера standalone dsh-edge (#43)» перенесён сюда автоматически, при механическом
# ребейзе, без содержательной правки run: — исходный комментарий шага
# (если был) приведён ниже дословно.
#
# Белое пятно #43 «второй пакетный менеджер поверх pnpm-дерева
# standalone»: npm install поверх дерева с семью обязательными для
# Workers pnpm-патчами пересобирает node_modules без патчей — зелёная
# сборка с молча неверным составом; --legacy-peer-deps маскирует то же
# место зелёным шагом. Гвардия: deploy-dsh-edge.yml не ставит пакеты
# npm'ом (npm pack префаба легален — дерево не трогает), флаг запрещён
# во всех workflow и scripts/*.sh, pnpm-маршрут плагинов (pnpm add
# --save-exact + проверка patchedDependencies) обязан оставаться.
set -euo pipefail
pip install --quiet pytest pyyaml
python -m pytest scripts/lib/test_dsh_edge_pnpm_guard.py -q
