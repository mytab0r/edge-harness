#!/usr/bin/env bash
# Реестр незакрытых находок ревью, ключ — файл (#1262, объединяет #1217):
# носитель после слияния PR сменился с задачи-хвоста пула
# (create_pool_issue, 0/110 закрытых) на файловый реестр (ветка данных
# data/review-findings) — см. ADR 0007, дельта 2026-09-14. Гвардия покрывает
# чистые функции реестра (load/dump/add/close/lookup, парсинг
# НАХОДКА-ЗАКРЫТА, маркер тела PR) на моке gh — сеть не нужна.
set -euo pipefail
pip install --quiet pytest
python -m pytest scripts/lib/test_review_findings.py -q
