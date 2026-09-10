#!/usr/bin/env bash
# Мигрировано из .github/workflows/repo-ci.yml в каталог гвардий (#749):
# шаг «Гвардия быстрого провайдера Claude anthropic-oauth-pool (#838)»
# добавлен независимо слитым PR ПОСЛЕ заморозки ALLOWLIST
# scripts/lib/ci_guard_registration_guard.py для PR #771 — перенос, не
# поднятие потолка.
#
# Быстрый провайдер Claude (#838, anthropic-oauth-pool) — независимо от
# suite/PLUGINS_SUITE_URL: без секретов пул не подключается (сеть не
# трогается), импорт секрета прод-кодом плагина кладёт корректный файл
# аккаунта И вычищает секрет из окружения (доверенная граница ai-review,
# #18 — ANTHROPIC_OAUTH_1/2 не подпадают под паттерн вырезания *_KEY/
# *_TOKEN/*_SECRET), пул первым/цепочка фоллбэком не ломает атрибуцию.
set -euo pipefail
bash scripts/lib/test/dsh-anthropic-pool.guard.sh
