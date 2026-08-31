#!/usr/bin/env bash
# Транспорт DSH для AI-ревью (#18): установка по пинам из lib, GLM-патч
# профиля, прогон headless. Ничего содержательного здесь не решается.
#
# Доверенная граница (trust-zone задачи #18): этому скрипту НЕ передаётся
# GitHub-токен — шаг workflow не имеет ни GH_TOKEN, ни git-креденшелов
# (checkout с persist-credentials: false). Агент физически не может
# запостить комментарий/метку/пуш: его единственный выход — файл ответа,
# который разбирает доверенный шаг verdict (ai_review.py verdict).
# DEEPSEEK_API_KEY нужен самому DSH для вызова модели; DSH вырезает env
# *TOKEN*/*KEY*/*SECRET* из model-shell вызовов — агент и его не видит
# (проверено живым прогоном 2026-08-30, см. worker.yml).
#
# Использование: AI_WORK=<каталог с prompt.md> bash scripts/review/ai_dsh.sh
# Результат: $AI_WORK/answer.txt (ответ агента), $AI_WORK/stderr.txt.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Пины DSH, integrity, GLM-патч профиля — единственное место правды в lib.
# shellcheck source=scripts/lib/dsh-ci.sh
source "$SCRIPT_DIR/../lib/dsh-ci.sh"

AI_WORK="${AI_WORK:?AI_WORK не задан (каталог с prompt.md и answer.txt)}"
DSH_TIMEOUT_SECS="${DSH_TIMEOUT_SECS:-3600}"   # 60 минут на ревью диффа
[ -f "$AI_WORK/prompt.md" ] || { echo "::error::нет $AI_WORK/prompt.md — шаг gather не отработал" >&2; exit 1; }
[ -n "${DEEPSEEK_API_KEY:-}" ] || { echo "::error::DEEPSEEK_API_KEY не задан — модель не будет вызвана" >&2; exit 1; }
export DEEPSEEK_API_KEY
export DEEPSEEK_BASE_URL="${DEEPSEEK_BASE_URL:-https://api.z.ai/api/coding/paas/v4}"

: >"$AI_WORK/answer.txt"; : >"$AI_WORK/stderr.txt"

dsh_install "$AI_WORK/pkgs"
dsh --version || true
dsh_patch_profile headless

# cwd = корень воркспейса (чекаут head PR) и не меняется — контракт dsh.
set +e
timeout "$DSH_TIMEOUT_SECS" dsh --profile headless "$(cat "$AI_WORK/prompt.md")" \
  >"$AI_WORK/answer.txt" 2>"$AI_WORK/stderr.txt"
rc=$?
set -e
echo "dsh завершился с кодом $rc"

# rc≠0 НЕ роняет шаг: судьбу решает доверенный verdict-шаг по содержимому
# ответа (пусто/битый контракт → ai:failed + красный job там). Хвост stderr —
# для диагностики в логе, ОБЯЗАТЕЛЬНО через redact: ошибки клиента модели —
# самое вероятное место, куда в публичный лог мог бы уехать производный
# DEEPSEEK_API_KEY (GitHub маскирует только точное совпадение секрета).
echo "--- хвост stderr DSH ---"
tail -c 2000 "$AI_WORK/stderr.txt" | redact || true
exit 0
