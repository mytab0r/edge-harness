#!/usr/bin/env bash
# Гвардия задачи #840: durable-скрипт экспорта Anthropic OAuth-учёток Claude
# (одиночный credentials.json ИЛИ krouter-бэкап providerConnections) в секрет
# ANTHROPIC_OAUTH_<slot> для dsh-anthropic-oauth-pool (#838).
#
# В репозиторий попала рукописным шагом repo-ci.yml и попала бы на ALLOWLIST
# scripts/lib/ci_guard_registration_guard.py, замороженный для PR #771, —
# поэтому регистрируется сразу в каталоге гвардий (#749): новый файл здесь,
# .github/workflows/repo-ci.yml не тронут ни строкой.
#
# Тесты на фейковых токенах (прод-формат credentials.json и krouter-бэкапа):
# валидация, отказы, формат секрета, главный инвариант — токены не утекают
# в stdout ни при каком прогоне.
set -euo pipefail
pip install --quiet pytest
python -m pytest scripts/lib/test_anthropic_oauth_import.py -q
