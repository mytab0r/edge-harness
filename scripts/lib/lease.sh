#!/usr/bin/env bash
# Аренда задачи (#121, ADR 0006) — единственное определение lease_cli для обоих
# клиентов (worker/task.sh, hands/dsh_task.sh). Единственный вход в работу над
# задачей пула: scripts/lib/claim_task.py (claim/release/locks).
#
# Токен: если канал приносит его в GH_RUN_TOKEN (hands.yml, contents:write на
# refs/locks/*), вызов аренды идёт под ним БЕЗ export — токен живёт только
# внутри этого вызова и не попадает в окружение прогона DSH (класс скраба
# *TOKEN* — см. worker.yml). Воркер наследует PAT из GH_TOKEN шага — ветка
# без GH_RUN_TOKEN просто передаёт окружение дальше.
lease_cli() {
  if [ -n "${GH_RUN_TOKEN:-}" ]; then
    GH_TOKEN="$GH_RUN_TOKEN" python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/claim_task.py" "$@"
  else
    python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/claim_task.py" "$@"
  fi
}
