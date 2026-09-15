#!/usr/bin/env bash
# Гвардия дрейфа vars.DSH_PROVIDER_CHAIN против config/provider-usage.json
# (#1289, доводка задачи про HTTP_410 — найдена координатором сразу после
# фикса: манифест нёс 8 провайдеров, переменная — 9, слот ZAI молча
# потерялся между #857 и #1067 и провисел так восемь дней до этого PR).
#
# Множество ИМЁН провайдеров в двух местах обязано совпадать — расхождение
# полей (model id/max_output_tokens) у общего имени НЕ гейтится здесь
# намеренно (манифест эволюционирует живыми проверками, переменная —
# исторический фоллбэк, см. докстринг test_provider_chain_var_drift_guard.py
# «Класс»). DSH_PROVIDER_CHAIN читается из env этого шага (перебор
# scripts/ci/guards, .github/workflows/repo-ci.yml) — контекстом workflow,
# без токена, тем же путём, что уже применяет dsh_require_provider_chain.
set -euo pipefail
pip install --quiet pytest
python -m pytest scripts/lib/test_provider_chain_var_drift_guard.py -q
python scripts/lib/test_provider_chain_var_drift_guard.py
