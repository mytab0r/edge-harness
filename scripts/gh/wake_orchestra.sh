#!/usr/bin/env bash
# Разбудить orchestra.yml, не тратя триггер впустую (#456).
#
# Почему: ADR 0012 сделал будильник безусловным нарочно — pr-review.yml и
# ai-review.yml дёргают `gh workflow run orchestra.yml` после КАЖДОГО своего
# прогона (568 раз/сутки по замеру #456), а concurrency-группа `orchestra`
# (cancel-in-progress: false) держит только ОДИН прогон в очереди на группу:
# при новом workflow_dispatch, пока прежний ещё выполняется/ждёт очереди,
# GitHub либо ставит новый в очередь поверх старого, либо (если очередь уже
# занята) отменяет более раннюю очередь в пользу новой. Итог — 35.6% из 500
# продиспатченных прогонов (замер #456) так и не стартовали: чистая трата
# вторичного лимита GitHub на content-generating запросы (500/час,
# docs/agents/INFRA-GH.md), а не потерянное событие — оркестратор читает
# состояние GitHub заново при каждом СТАРТЕ, не снимок на момент диспатча.
#
# Что чинит этот скрипт: перед диспатчем — один дешёвый GET (не расходует
# content-generating квоту) на список прогонов orchestra.yml в состоянии
# in_progress/queued. Уже есть хотя бы один — новый диспатч не нужен: тот же
# самый прогон, когда стартует, увидит уже случившееся событие (метку
# review:ok/changes-requested и т.п.) точно так же, как увидел бы прогон,
# запущенный нашим диспатчем — оно уже записано на сервере ДО этого шага.
#
# Газ (не тормоз без возврата): если GET сам не ответил (сеть/лимит) —
# fail-safe в СТОРОНУ ADR 0012, не в сторону экономии: диспатчим как раньше,
# без проверки. Пропущенное событие дороже одного лишнего триггера.
#
# Использование: GH_TOKEN=... scripts/gh/wake_orchestra.sh <owner/repo>
set -euo pipefail

repo="${1:?использование: wake_orchestra.sh <owner/repo>}"

has_active_run() {
  local status="$1"
  local payload
  if ! payload=$(gh api "repos/$repo/actions/workflows/orchestra.yml/runs?status=$status&per_page=1" 2>&1); then
    return 2  # сеть/лимит — сигнал "не знаю", не "нет"
  fi
  [ "$(printf '%s' "$payload" | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("workflow_runs", [])))' 2>/dev/null || echo 0)" != "0" ]
}

for status in in_progress queued; do
  rc=0
  has_active_run "$status" || rc=$?
  if [ "$rc" -eq 0 ]; then
    echo "orchestra.yml уже $status — диспатч пропущен (следующий старт увидит это же событие, #456)"
    exit 0
  elif [ "$rc" -eq 2 ]; then
    echo "::warning::проверка активных прогонов orchestra.yml ($status) не удалась — диспатчу без проверки (fail-safe в сторону ADR 0012)"
    gh workflow run orchestra.yml --ref main
    exit 0
  fi
done

gh workflow run orchestra.yml --ref main
