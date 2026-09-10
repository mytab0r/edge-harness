#!/usr/bin/env bash
# Мигрировано из .github/workflows/repo-ci.yml в каталог гвардий (#749):
# шаг «Тесты гвардии кодировки stdout/subprocess (Windows)» добавлен
# независимо слитым PR ПОСЛЕ заморозки ALLOWLIST
# scripts/lib/ci_guard_registration_guard.py для PR #771 — перенос, не
# поднятие потолка.
#
# Класс «Windows-кодировка валит скрипт репозитория» (issue #723): print()
# в stdout без file=sys.stderr падает UnicodeEncodeError (эмодзи, cp1251);
# subprocess.run(text=True) без encoding="utf-8" падает UnicodeDecodeError
# в фоновом потоке на кириллице реального ответа gh (живой трейс: task-branch
# → epic_guard.py → scheduler.gh → pulse_guard.gh). Гвардия статическая
# (по исходнику, весь класс) + живая мутация pulse_guard.gh на прод-форме
# байта, ронявшего cp1251.
set -euo pipefail
pip install --quiet pytest pyyaml
python -m pytest scripts/lib/test_console_utf8_guard.py -q
