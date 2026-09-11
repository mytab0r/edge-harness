#!/usr/bin/env bash
# Детектор регрессии здоровья конвейера, привязанный к слиянию (issue #967,
# живой корень #878/#937): часовое rolling-сравнение recent/baseline
# worker_success_rate + подозреваемые слияния в окне атрибуции + эскалация/
# задача пула. Тесты — чистая логика (мутация порогов regression/fire),
# суспекты по прод-форме `search/issues`, и, отдельным блоком, ДОСЛОВНАЯ
# история `worker.yml` вокруг PR #878 (2026-09-10/11) — доказательство,
# что детектор поймал бы регрессию в пределах часов, не суток.
set -euo pipefail
pip install --quiet pytest pyyaml
python -m pytest scripts/orchestra/test_merge_health_watch.py -q
