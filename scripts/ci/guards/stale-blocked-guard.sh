#!/usr/bin/env bash
# Мигрировано из .github/workflows/repo-ci.yml как первое доказательство
# каталога гвардий (#749). Гвардия протухшей метки blocked (#334): без
# прогона тестов здесь она живёт только текстом файла — по правилу
# репозитория «решение — это механизм, а не текст» (AGENTS.md) это ровно
# тот класс, который она сама закрывает для остальных, но изначально не
# подключала себе (находка AI-ревью PR #336, третий раунд):
# continue-on-error в orchestra.yml держит job зелёным при любой поломке
# STALE_MARKER_RE/find_stale_blocked, и без обязательного прогона тестов
# здесь это никто не заметит.
set -euo pipefail
pip install --quiet pytest pyyaml
python -m pytest scripts/orchestra/test_stale_blocked_guard.py -q
