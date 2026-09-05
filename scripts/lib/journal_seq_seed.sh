#!/usr/bin/env bash
# Посев journal-seq максимумом с сервера по всем страницам (находка ревью
# PR #241, п.1): без посева повторный прогон job'а под тем же TASK_ID
# (владелец нажал "Re-run failed jobs", ручной перезапуск с тем же run_id)
# начинает нумерацию с 0 — сервер молча отбрасывает дубликаты по
# UNIQUE(task_id, seq) (harness.ts, POST /api/events: duplicates считается,
# #applySideEffects — только для принятых), и события успешного повтора
# невидимы в журнале, а задача остаётся в очереди навсегда как failed.
# Максимум берётся по ВСЕМ источникам под task_id (не только своему), иначе
# чужое событие (статусы, другая автоматизация) под тем же task_id
# переиспользовало бы его seq (тот же класс, что уже закрыт в
# scripts/hands/dsh_task.sh).
#
# Использование: source этот файл, затем `journal_seq_seed` — печатает в
# stdout максимум seq по TASK_ID (0, если событий ещё нет). Требует в
# окружении HANDS_URL, HANDS_TOKEN, TASK_ID; CURL_CONNECT_TIMEOUT/
# CURL_MAX_TIMEOUT — опциональны (дефолт 5/30, как у вызывающих).
journal_seq_seed() {
  local after=0 max=0 resp n ms has_more
  while :; do
    resp=$(curl -fsS --connect-timeout "${CURL_CONNECT_TIMEOUT:-5}" --max-time "${CURL_MAX_TIMEOUT:-30}" \
      -H "Authorization: Bearer $HANDS_TOKEN" \
      "$HANDS_URL/api/events?task_id=$TASK_ID&after=$after&limit=256") || return 1
    n=$(jq '.events | length' <<<"$resp")
    if [ "$n" -eq 0 ]; then break; fi
    ms=$(jq '[.events[] | .seq] | max // 0' <<<"$resp")
    if [ "$ms" -gt "$max" ]; then max=$ms; fi
    has_more=$(jq -r '.has_more' <<<"$resp")
    after=$(jq -r '.next_after' <<<"$resp")
    if [ "$has_more" != "true" ]; then break; fi
  done
  printf '%s\n' "$max"
}
