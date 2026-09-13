#!/usr/bin/env bash
# Перенесено из .github/workflows/repo-ci.yml транслятором рукописных
# шагов гвардий (issue #897, продолжение #749/#771/#762/#764): исходный
# шаг «Гвардия «газ метки достижим правкой тела PR»» перенесён сюда автоматически, при механическом
# ребейзе, без содержательной правки run: — исходный комментарий шага
# (если был) приведён ниже дословно.
#
# Класс «метку ставит событие A, снимает только событие B, не
# покрывающее способ реального исправления» (#599, живой случай PR
# #586: contract:failed висел вечно, потому что правка ТЕЛА PR не
# входила в pull_request.types job'а contract). Гвардия сканирует
# .github/workflows/*.yml на job'ы, читающие тело PR и удаляющие метку,
# без edited в pull_request.types — новый такой job красит CI, если не
# назван явно в ALLOWLIST с номером задачи.
set -euo pipefail
pip install --quiet pytest pyyaml
python -m pytest scripts/lib/test_pr_body_label_release_reachable.py -q
