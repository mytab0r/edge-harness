#!/usr/bin/env bash
# Наследование области хвостом чеклиста ревью (сирота A, аудит владельца
# 2026-09-11 — 46 задач «Хвост чеклиста ревью PR #N», закрыто 0): тело
# каждой такой задачи обещает «Область и приоритет — как у породившего PR»,
# но create_pool_issue заводит её только с меткой task. Гвардия покрывает
# чистые функции (inheritable_labels/apply_inherited_labels) на моке gh —
# сеть не нужна.
set -euo pipefail
pip install --quiet pytest
python -m pytest scripts/orchestra/test_checklist_tail_labels.py -q
