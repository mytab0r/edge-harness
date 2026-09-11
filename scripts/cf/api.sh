#!/usr/bin/env bash
# Безопасный произвольный GET к Cloudflare API v4 — для разовых проверок,
# которые не попали в status.sh.
#
# Использование: scripts/cf/api.sh /accounts/{account_id}/workers/scripts/edge-harness/deployments
# (не account-wide листинг — по конкретному нашему воркеру; см. ниже про листинги)
# Секреты берутся из CLOUDFLARE_API_TOKEN/CLOUDFLARE_ACCOUNT_ID в окружении.
set -euo pipefail
dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/cf/lib.sh
source "$dir/lib.sh"

if [ $# -lt 1 ]; then
  echo "Использование: $0 <path-после-/client/v4>" >&2
  exit 1
fi

cf_require_env || exit 1

# Отказ до allowlist на пути с dot-сегментами/кодированием. curl по умолчанию
# схлопывает `../` (проверено живым curl: `/a/edge-harness/../../zones` уходит
# на сервер как `/a/zones`), значит путь
# `/accounts/…/workers/scripts/edge-harness/../../zones` матчится allowlist-гейтом
# ниже по литеральной строке, а по сети это account-wide `/zones` — класс
# инцидента 2026-09-05 (находка ревью PR #328). Легитимные пути v4 по нашим
# воркерам не требуют ни кодирования, ни повторных слешей — всё перечисленное
# отклоняем до сети, opt-in здесь не действует.
case "$1" in
  *..*|*//*|*\\*|*%*)
    echo "ОТКАЗ: $1 — путь с '..', '//', обратным слешем или '%' отклоняется до allowlist: curl схлопывает dot-сегменты, такой путь может уйти на сервер другим (обход allowlist). Используй канонический путь без кодирования." >&2
    exit 1
    ;;
esac

# Класс инцидента 2026-09-05 (чужие имена/значения account-wide листингов в
# публичный лог): блок-лист конкретных путей (первая версия этого файла) не
# закрывал класс — любой НЕзаблокированный account-wide путь или под-ресурс
# чужого воркера («/workers/services», «/workers/scripts/<чужой>/content»,
# «/deployments», «/user» без «/tokens/verify») печатал бы сырьё дальше.
# Инвертировано: разрешён только явный allowlist (проверка токена + пути по
# НАШИМ воркерам из CF_OWN_WORKERS), всё остальное — отказ по умолчанию, а не
# «пока не попало в блок-лист». Опасный путь всё ещё доступен явным opt-in
# (CF_API_SH_ALLOW_RAW=1) — печатает сырьё, не для публичного лога.
allowed=0
case "$1" in
  /user/tokens/verify)
    allowed=1
    ;;
  *)
    for own in $CF_OWN_WORKERS; do
      case "$1" in
        */workers/scripts/"$own"/*|*/workers/scripts/"$own")
          allowed=1
          break
          ;;
      esac
    done
    ;;
esac

if [ "$allowed" != 1 ]; then
  if [ "${CF_API_SH_ALLOW_RAW:-}" = "1" ]; then
    echo "ПРЕДУПРЕЖДЕНИЕ: $1 — путь вне allowlist (не наш воркер), CF_API_SH_ALLOW_RAW=1 печатает сырьё как есть. Не для публичного лога." >&2
  else
    echo "ОТКАЗ: $1 — путь вне allowlist по умолчанию (только /user/tokens/verify и наши воркеры из CF_OWN_WORKERS в lib.sh). Account-wide листинги — через scripts/cf/status.sh (cf_count_only/cf_workers_own/cf_do_namespaces_own/cf_bindings_names). Опасный явный путь — CF_API_SH_ALLOW_RAW=1 (печатает сырьё, не для публичного лога)." >&2
    exit 1
  fi
fi

cf_get "$1"
