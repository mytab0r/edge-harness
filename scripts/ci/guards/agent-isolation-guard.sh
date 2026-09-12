#!/usr/bin/env bash
# Гвардия #140: dsh обязан идти под агент-юзером без группы docker.
# Исполняет продакшн-путь dsh_agent_isolation_prepare с настоящим sudo
# на одноразовом юзере и проверяет свойства как факты окружения:
# env_keep проводит секрет, environ транспорта и docker агенту закрыты,
# nogh-граница #18 цела, подготовка идемпотентна. Нужны sudo и docker —
# как на GitHub-раннерах; локальная машина без sudo красит гвардию честно.
#
# Регистрация каталогом scripts/ci/guards/ (#749), не рукописный шаг в
# repo-ci.yml — конфликт сведения PR #395/задача #140 при ребейзе на main,
# где #749 уже запретил рукописные шаги (см. ALLOWLIST
# scripts/lib/ci_guard_registration_guard.py).
set -euo pipefail
bash scripts/lib/test/agent-isolation.guard.sh
