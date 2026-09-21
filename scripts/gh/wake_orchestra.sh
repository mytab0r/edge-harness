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
# ВОЗРАСТ активного прогона (issue #1408). Экономия верна для прогона,
# который ещё МОЖЕТ стартовать, и неверна для зависшего: тот не стартует
# никогда, а пропуск случается каждый раз. Живой случай — прогон 34748469966
# висел в `queued` с 2026-09-13T08:46:40Z, восемь суток, с нулём job'ов;
# `POST .../cancel` и `.../force-cancel` отвечают на него HTTP 409 «Cannot
# cancel a workflow run that has not been queued yet», то есть он неубиваем
# и через API (класс #1106). Всё это время КАЖДЫЙ вызов будильника — а его
# зовут pr-review.yml и ai-review.yml после каждого своего прогона, около
# 568 раз в сутки по замеру #456 — уходил в пропуск, и у оркестратора
# оставался один живой триггер, `schedule`. GitHub задерживает и роняет
# scheduled-прогоны на нагруженном репозитории: фактические запуски по
# расписанию шли раз в 2–5 часов вместо заявленных 15 минут, а слияния при
# зелёных mergeable PR простаивали по два часа.
#
# Поэтому прогон старше порога активным НЕ считается. Пороги разные и оба
# названы: очередь дольше QUEUED_STALE_MINUTES — это зомби (нормальное
# ожидание в очереди — секунды-минуты); выполнение дольше
# IN_PROGRESS_STALE_MINUTES — тоже (проход оркестратора укладывается в
# единицы минут). Возраст неизвестен (ответ без created_at) — считаем
# протухшим: fail-safe в ту же сторону ADR 0012, что и сбой проверки.
#
# Зомби этим НЕ убивается — он неубиваем; снимается только его роль
# тормоза без газа. Сам зомби и watchdog к нему — открытая #1106.
#
# Использование: GH_TOKEN=... scripts/gh/wake_orchestra.sh <owner/repo>
set -euo pipefail

repo="${1:?использование: wake_orchestra.sh <owner/repo>}"
QUEUED_STALE_MINUTES="${WAKE_QUEUED_STALE_MINUTES:-10}"
IN_PROGRESS_STALE_MINUTES="${WAKE_IN_PROGRESS_STALE_MINUTES:-60}"

# Печатает «none», «fresh <возраст-минут>» или «stale <возраст-минут>».
# Код 2 — «не знаю» (сеть/лимит), обрабатывается вызывающим как прежде.
active_run_kind() {
  local status="$1" limit="$2"
  local payload
  if ! payload=$(gh api "repos/$repo/actions/workflows/orchestra.yml/runs?status=$status&per_page=1" 2>&1); then
    return 2  # сеть/лимит — сигнал "не знаю", не "нет"
  fi
  printf '%s' "$payload" | python3 -c '
import json, sys
from datetime import datetime, timezone

limit = float(sys.argv[1])
try:
    runs = (json.load(sys.stdin) or {}).get("workflow_runs") or []
except Exception:
    print("none")
    raise SystemExit(0)
if not runs:
    print("none")
    raise SystemExit(0)
raw = runs[0].get("created_at")
if not raw:
    # Возраст неизвестен — тот же fail-safe, что и сбой проверки: лишний
    # триггер дешевле отключённого будильника (#1408).
    print("stale ?")
    raise SystemExit(0)
age = (datetime.now(timezone.utc)
       - datetime.fromisoformat(raw.replace("Z", "+00:00"))).total_seconds() / 60
print(("stale" if age > limit else "fresh"), f"{age:.0f}")
' "$limit" 2>/dev/null || echo "none"
}

dispatch() {
  gh workflow run orchestra.yml --ref main
}

for entry in "in_progress:$IN_PROGRESS_STALE_MINUTES" "queued:$QUEUED_STALE_MINUTES"; do
  status="${entry%%:*}"
  limit="${entry##*:}"
  rc=0
  kind=$(active_run_kind "$status" "$limit") || rc=$?
  if [ "$rc" -eq 2 ]; then
    echo "::warning::проверка активных прогонов orchestra.yml ($status) не удалась — диспатчу без проверки (fail-safe в сторону ADR 0012)"
    dispatch
    exit 0
  fi
  case "$kind" in
    fresh*)
      echo "orchestra.yml уже $status — диспатч пропущен (следующий старт увидит это же событие, #456)"
      exit 0
      ;;
    stale*)
      echo "::warning::orchestra.yml числится $status ${kind#stale } мин (порог $limit) — прогон завис и не стартует; диспатчу, а не пропускаю (#1408, класс #1106)"
      dispatch
      exit 0
      ;;
  esac
done

dispatch
