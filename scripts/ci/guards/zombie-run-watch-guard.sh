#!/usr/bin/env bash
# Сторож зомби-прогонов PR-чеков (issue #1106, живой случай PR #1088): run
# застрял `queued` с нулём job'ов, `gh run rerun` отказывает (нечего
# перезапускать), watchdog'а не было. Газ — переэмиссия событий PR
# (close→reopen, head SHA не меняется), один раз на head_sha; рецидив ПОСЛЕ
# переэмиссии — эскалация, не повторный retry. Тесты кормятся дословным
# снимком живых зомби-прогонов PR #1088 (2026-09-14).
set -euo pipefail
pip install --quiet pytest pyyaml
python -m pytest scripts/orchestra/test_zombie_run_watch.py -q
