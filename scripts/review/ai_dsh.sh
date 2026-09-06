#!/usr/bin/env bash
# Транспорт DSH для AI-ревью (#18): установка по пинам из lib, GLM-патч
# профиля, прогон headless. Ничего содержательного здесь не решается.
#
# Доверенная граница (trust-zone задачи #18): этому скрипту НЕ передаётся
# GitHub-токен — шаг workflow не имеет ни GH_TOKEN, ни git-креденшелов
# (checkout с persist-credentials: false). Агент физически не может
# запостить комментарий/метку/пуш: его единственный выход — файл ответа,
# который разбирает доверенный шаг verdict (ai_review.py verdict).
# DEEPSEEK_API_KEY нужен самому DSH для вызова модели. Вырезание env
# *TOKEN*/*KEY*/*SECRET* из model-shell вызовов само по себе границей не
# является (#140): ключ читается из environ родителя через docker-эскейп.
# Здесь та же изоляция, что у worker/hands: dsh идёт под агент-юзером без
# docker, в режиме nogh — без зеркала gh-конфига, его отсутствие проверяется
# (docs/research/40-model-shell-key-exposure.md).
#
# Использование: AI_WORK=<каталог с prompt.md> bash scripts/review/ai_dsh.sh
# Результат: $AI_WORK/answer.txt (ответ агента), $AI_WORK/stderr.txt,
# $AI_WORK/dsh_rc.txt (код возврата dsh — единственный сигнал, различающий
# «транспорт упал» от «дсш вернул текст»; смотри verdict в ai_review.py).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Пины DSH, integrity, GLM-патч профиля — единственное место правды в lib.
# shellcheck source=scripts/lib/dsh-ci.sh
source "$SCRIPT_DIR/../lib/dsh-ci.sh"

AI_WORK="${AI_WORK:?AI_WORK не задан (каталог с prompt.md и answer.txt)}"
DSH_TIMEOUT_SECS="${DSH_TIMEOUT_SECS:-3600}"   # 60 минут на ревью диффа
[ -f "$AI_WORK/prompt.md" ] || { echo "::error::нет $AI_WORK/prompt.md — шаг gather не отработал" >&2; exit 1; }
# Одно место правды — vars.DEEPSEEK_BASE_URL/DEEPSEEK_MODEL репозитория (#153):
# зашитых фолбэков на конкретный эндпоинт/модель здесь больше нет.
dsh_require_provider_env || exit 1
export DEEPSEEK_API_KEY DEEPSEEK_BASE_URL DEEPSEEK_MODEL

: >"$AI_WORK/answer.txt"; : >"$AI_WORK/stderr.txt"

dsh_install "$AI_WORK/pkgs"
# --version — от транспорта: бинарник только читается, секретов в нём нет (#140).
dsh --version || true

# Изоляция #140, режим nogh: у ревью-агента не должно быть gh-авторизации
# (граница #18) — подготовка сносит протухшее зеркало и проверяет отсутствие.
AI_AGENT_DIR="$AI_WORK/agent"
dsh_agent_isolation_prepare nogh "$(pwd)" "$AI_AGENT_DIR" "$AI_WORK/dsh-agent-launcher.sh"
dsh_patch_profile headless "$AI_WORK/agent-headless.cordis.patch.yml"
dsh_agent_run install -D -m 644 "$AI_WORK/agent-headless.cordis.patch.yml" \
  "$DSH_AGENT_HOME/.dsh/profiles/headless/cordis.patch.yml"

# cwd = pr-head (дерево PR — ДАННЫЕ агента; доверенный код лежит в main-чекауте
# воркспейса) и не меняется до конца прогона — контракт dsh.
set +e
timeout "$DSH_TIMEOUT_SECS" dsh_agent_run dsh --profile headless "$(cat "$AI_WORK/prompt.md")" \
  >"$AI_WORK/answer.txt" 2>"$AI_WORK/stderr.txt"
rc=$?
set -e
echo "dsh завершился с кодом $rc"
printf '%s' "$rc" >"$AI_WORK/dsh_rc.txt"

# rc≠0 НЕ роняет ЭТОТ шаг: судьбу решает доверенный verdict-шаг. Но rc едет
# дальше НЕзамаскированным сигналом (dsh_rc.txt) — verdict обязан отличить
# «транспорт упал (rc≠0)» от «дсш вернул текст не по контракту (rc=0)»,
# иначе ошибка провайдера превращается в ложное обвинение модели (silent-wrong).
# Хвост stderr — для диагностики в логе, ОБЯЗАТЕЛЬНО через redact: ошибки
# клиента модели — самое вероятное место, куда в публичный лог мог бы уехать
# производный DEEPSEEK_API_KEY (GitHub маскирует только точное совпадение секрета).
echo "--- хвост stderr DSH ---"
tail -c 2000 "$AI_WORK/stderr.txt" | redact || true
exit 0
