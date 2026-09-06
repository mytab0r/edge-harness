#!/usr/bin/env bash
# Остаток основных лимитов GitHub API (core/graphql/search), понятным текстом.
# Вторичный лимит content-generating (500/час на repository_dispatch/
# workflow_dispatch) GitHub НЕ публикует через этот эндпоинт — см.
# docs/agents/INFRA-GH.md и docs/research/21-github-actions.md.
set -euo pipefail

data=$(gh api rate_limit) || {
  echo "ОШИБКА: gh api rate_limit не ответил — сеть/токен." >&2
  exit 1
}

printf '%s' "$data" | PYTHONIOENCODING=utf-8 python3 -c '
import json, sys, datetime
d = json.load(sys.stdin)["resources"]
for key in ("core", "graphql", "search"):
    remaining = d[key]["remaining"]
    limit = d[key]["limit"]
    reset = datetime.datetime.fromtimestamp(d[key]["reset"]).strftime("%H:%M:%S")
    print(f"{key:8s} {remaining:5d}/{limit:<5d} сброс {reset}")
'
echo "Вторичный лимит 500 content-generating запросов/час (dispatch) — без счётчика в API, см. docs/agents/INFRA-GH.md."
