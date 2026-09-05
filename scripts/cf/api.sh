#!/usr/bin/env bash
# Безопасный произвольный GET к Cloudflare API v4 — для разовых проверок,
# которые не попали в status.sh.
#
# Использование: scripts/cf/api.sh /accounts/{account_id}/workers/scripts
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
cf_get "$1"
