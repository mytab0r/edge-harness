#!/usr/bin/env bash
# Перенесено из .github/workflows/repo-ci.yml транслятором рукописных
# шагов гвардий (issue #897, продолжение #749/#771/#762/#764): исходный
# шаг «Гвардия разделения GitHub-токенов» перенесён сюда автоматически, при механическом
# ребейзе, без содержательной правки run: — исходный комментарий шага
# (если был) приведён ниже дословно.
#
# Разделение GitHub-токенов (задача #6, ADR 0008): узкий GH_DISPATCH_TOKEN
# (fine-grained, морда) упоминает только deploy-worker.yml и синх
# сохранён; широкие потребители (worker/orchestra/deploy-dsh-edge/probe)
# — на GH_PIPELINE_PAT. Регресс «подсел на dispatch-токен» красит CI,
# а не молча ломает конвейер следующим сужением. Доказана мутацией:
# каждое из трёх правил красится своей правкой.
set -euo pipefail
pip install --quiet pytest pyyaml
python -m pytest scripts/lib/test_dispatch_token_usage.py -q
