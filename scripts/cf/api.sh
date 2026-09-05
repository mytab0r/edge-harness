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

# Класс инцидента 2026-09-05 (чужие имена/значения account-wide листингов в
# публичный лог): известные account-wide пути печатают сырьё без фильтра, а
# этот скрипт — единственная лазейка вокруг фильтров status.sh. Известные
# пути отклоняем явно; для них уже есть безопасная обёртка в lib.sh/status.sh.
case "$1" in
  */workers/scripts|*/workers/scripts/)
    echo "ОТКАЗ: $1 — account-wide список воркеров, печатает чужие имена. Используй scripts/cf/status.sh (cf_workers_own) или jq-фильтр." >&2
    exit 1
    ;;
  */workers/scripts/*/settings)
    echo "ОТКАЗ: $1 — settings воркера может содержать значения plain_text bindings. Используй scripts/cf/status.sh (cf_bindings_names)." >&2
    exit 1
    ;;
  */workers/durable_objects/namespaces|*/storage/kv/namespaces|*/d1/database|*/r2/buckets|*/zones)
    echo "ОТКАЗ: $1 — account-wide список, печатает чужие id/имена. Используй scripts/cf/status.sh (cf_count_only/cf_do_namespaces_own)." >&2
    exit 1
    ;;
esac

cf_get "$1"
