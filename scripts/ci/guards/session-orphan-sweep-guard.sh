#!/usr/bin/env bash
# Sweep осиротевших сессий морды dsh-edge (#940): классификация
# session.list по паттерну harness-<N>[-r<run>] + статус задачи — чистая
# функция `classify_sessions`, тестируется прод-формой (сама конвенция
# session_id, scripts/worker/task.sh:472/478), плюс поведенческий тест
# логина на настоящем HTTP-сервере (тот же контракт 303 + Set-Cookie +
# UA-фильтр, что test_scheduler.py::login_server) — не текст исходника.
set -euo pipefail
pip install --quiet pytest
python -m pytest scripts/orchestra/test_session_orphan_sweep.py -q
