#!/usr/bin/env bash
# Канарейка dispatch-пути морды (задача #6): доказательство видимым результатом,
# а не кодом ответа. POST /api/tasks → в ответе dispatched:true → появился run
# workflow hands с событием repository_dispatch, созданный ПОСЛЕ запроса.
# 204 от GitHub — не доказательство запуска (docs/research/21-github-actions.md,
# «ловушка default-branch»), поэтому ждём именно run.
#
# Применение:
#   - пост-мерж проверка задачи #6 после установки узкого GH_DISPATCH_TOKEN
#     (runbook — комментарий в задаче #6 и ADR 0008);
#   - разовая канарейка контура «морда → руки» в любой момент.
#
# Среда:
#   HARNESS_URL  — публичный URL воркера (vars.HARNESS_URL)
#   HANDS_TOKEN  — секрет HANDS_TOKEN (Bearer для /api/tasks)
#   GH_TOKEN     — gh для проверки run'ов (не обязан совпадать с dispatch-токеном)
#   TIMEOUT_SECS — ожидание старта run'а, по умолчанию 300 (хвост очереди
#                  GitHub Actions тяжёлый и ничем не ограничен сверху —
#                  research/21; таймаут канарейки обязан быть конечным)
set -euo pipefail

HARNESS_URL=${HARNESS_URL:?нужен HARNESS_URL (vars.HARNESS_URL)}
HANDS_TOKEN=${HANDS_TOKEN:?нужен HANDS_TOKEN (секрет репозитория)}
: "${GH_TOKEN:?нужен GH_TOKEN для gh run list}"
TIMEOUT_SECS=${TIMEOUT_SECS:-300}

repo=$(gh repo view --json nameWithOwner --jq .nameWithOwner)
# Запас −30с: since сравнивается с createdAt run'а ПО ЧАСАМ СЕРВЕРА GitHub —
# расхождение локальных часов вперёд даже на секунду выкинет свежий run из
# фильтра, и канарейка соврёт «run не появился» (находка AI-ревью #146).
since=$(date -u -d '-30 seconds' +%Y-%m-%dT%H:%M:%SZ)

echo "== POST $HARNESS_URL/api/tasks (отправка от $since UTC)"
response=$(curl -sS -w '\n%{http_code}' -X POST "$HARNESS_URL/api/tasks" \
  -H "Authorization: Bearer $HANDS_TOKEN" \
  -H 'Content-Type: application/json' \
  --data '{"payload":{"task":"канарейка dispatch-пути #6: подтверди получение одной строкой и заверши сессию, изменений не делай","canary":"dispatch-token","note":"канарейка задачи #6: подтверждение dispatch-пути"}}')

http_code=$(tail -n1 <<<"$response")
body=$(sed '$d' <<<"$response")
[ "$http_code" = "201" ] || { echo "::error::POST /api/tasks → HTTP $http_code: $body"; exit 1; }

# Разбор ответа прод-формы (см. harness.ts #postTask): python3, как весь CI-стек.
read -r task_id dispatched dispatch <<<"$(python3 - "$body" <<'PY'
import json, sys
d = json.loads(sys.argv[1])
print(d.get("task_id", ""), str(d.get("dispatched", "")).lower(), d.get("dispatch", ""))
PY
)"
echo "   task_id=$task_id dispatched=$dispatched dispatch=$dispatch"

if [ "$dispatched" != "true" ]; then
  echo "::error::dispatch не состоялся: dispatched=$dispatched dispatch=$dispatch."
  [ "$dispatch" = "not_configured" ] && echo "   (в воркере нет GH_DISPATCH_TOKEN/GH_REPO — «возможности нет», чинится деплоем)"
  exit 1
fi

echo "== Жду run hands (repository_dispatch) не старше $since, таймаут ${TIMEOUT_SECS}s"
deadline=$(( $(date +%s) + TIMEOUT_SECS ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  run_url=$(gh run list --repo "$repo" --workflow hands.yml --event repository_dispatch \
    --limit 5 --json createdAt,url --jq "[.[] | select(.createdAt >= \"$since\")][0].url // \"\"")
  case "$run_url" in
    http*)
      echo "   run появился: $run_url"
      echo "CANARY OK: POST /api/tasks → dispatched:true → run $run_url"
      exit 0
      ;;
  esac
  sleep 10
done

echo "::error::run hands не появился за ${TIMEOUT_SECS}s. Диспатч принят (204), но run нет —"
echo "   проверять обе причины из research/21: workflow-файл на default branch и права токена"
echo "   (узкому токену нужны Contents:write И Actions:write — пульс воркера зовёт"
echo "   workflow_dispatch оркестратора и деплоя)."
exit 1
