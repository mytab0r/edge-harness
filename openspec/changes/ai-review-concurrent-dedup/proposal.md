# ai-review-concurrent-dedup: один PR — не больше одного летящего ai-review (#779)

Задача: #779. Дельта-спека:
[specs/journal-tasks-hands/spec.md](specs/journal-tasks-hands/spec.md).

## Зачем

Три источника поднимают `ai-review.yml` на один и тот же PR (событийный
`workflow_run` от `pr-review`, ручной `workflow_dispatch`, автоповтор #196
`scheduler.trigger_ai_review`), а concurrency-группа событийного пути была
ключевана `workflow_run.id` — уникальным на КАЖДОЕ событие `pr-review`, не
на номер PR. Два триггера одного PR не сериализовались вовсе (замер #779:
45 пересекающихся по времени пар прогонов одного PR за окно наблюдения, 4
из них — с разницей меньше 20 секунд). Единственная сохранившаяся страховка
(`review_labels.other_active_ai_review_runs`, «прогон уже летит») спрашивалась
до этой правки только ручным путём — событийный путь (68 из 134 прогонов в
суточном замере) её не спрашивал совсем, и симметрично отказывал бы сам
себе (оба прогона видят друг друга `in_progress` и оба печатают `false` —
живой случай PR #725, 2026-09-08, run 34183129865/34183131439, Δ=2с:
итог — ноль вердиктов, не один).

## Что делается

- Ключ `concurrency.group` в `ai-review.yml` — номер PR для ОБОИХ путей
  триггера (та же пара выражений, что уже вычисляет `run-name`), фолбэк на
  `workflow_run.id` только для форк-PR (пусто `pull_requests[0]`).
  `cancel-in-progress: false` — второй триггер ждёт в очереди, не отменяет
  летящий прогон (не теряет уже посчитанный вердикт); цена явно названа —
  один ожидающий слот на группу, третий триггер того же PR отменяет только
  второй, ещё не стартовавший.
- `review_labels.other_active_ai_review_runs` спрашивается ОБОИМИ путями
  триггера в `ai_review.py::cmd_should_run` (было — только ручным), с
  разными текстами отказа для разных читателей: `manual_dispatch_busy_reason`/
  `event_dispatch_duplicate_reason` (прогон летит) и
  `manual_dispatch_skip_reason`/`event_dispatch_skip_reason` (дифф не
  изменился с последнего вердикта) — читатель различает причину, не гадает.
- Потолок возраста `AI_REVIEW_TIMEOUT_MINUTES` (то же число, что
  `timeout-minutes:` job'а `review`) — прогон старше потолка по `created_at`
  больше не читается как «летит» ни в одном из трёх мест
  (`other_active_ai_review_runs`, `trigger_ai_review`, `retry_budget_fact`):
  без потолка занятость и автоповтор глохли бы на весь возраст зависшего
  прогона, а не только на реальное время его жизни.
- Третье состояние инварианта 3 (`repo_invariants.retry_budget_fact` /
  `stuck_gate_fact_line`): бюджет автоповтора ещё есть, но летящий прогон
  придерживает его (`held_back_by_run`) — строка факта называет именно это,
  в ОБЕИХ ветках, где бюджет не исчерпан (`total < limit` и сестринская
  `total >= limit, in_epoch < limit`, перенос из прошлой эпохи, #431/#472),
  а не молчит «должен сработать сам», когда тик на самом деле сделает
  `continue` и не тронет бюджет.

## Проверено

- `python -m pytest scripts/review scripts/orchestra scripts/lib -q` —
  зелёные (кроме `test_redact_*`, известный дефект среды Windows/WSL, не
  регрессия — воспроизводится на чистом `main`).
- Мутация: `timeout-minutes: 1300` у job `review` и перенос потолка на job
  `verdict` — оба красят `test_ai_review_timeout_minutes_matches_review_
  labels_constant` (yaml.safe_load по ключу `jobs.review.timeout-minutes`,
  не подстрока).
- Мутация: вернуть `if not run_needed and manual` (снять печать причины для
  событийного пути) — красит
  `test_cmd_should_run_prints_false_when_diff_unchanged_ai_ok`.
- Мутация: убрать ветку `held_back_by_run` из сестринской ветки
  `stuck_gate_fact_line` (`total >= limit, in_epoch < limit`) — красит
  `test_stuck_gate_fact_line_budget_carried_over_and_held_back_by_active_run`.
- Мутация: `concurrency.cancel-in-progress: true` — красит
  `test_ai_review_concurrency_does_not_cancel_in_progress`.

## Что вне рамок

- Форк-PR: сегодня в репозитории нет (прочёс 1425 прогонов ai-review.yml —
  все голые `display_title` до внесения `run-name`, в свежих 600 аномалий
  ноль, `head_repository` у всех свой). Граница названа явно
  (`review_labels.other_active_ai_review_runs`, `ai-review.yml`): на форк-PR
  оба тормоза (занятость и очередь concurrency) деградируют в один и тот же
  fallback и выключаются одновременно — не устранено, только названо.
- Статус `pending` (создаётся новым ключом группы ВПЕРВЕ в истории
  репозитория — второй триггер того же PR ждёт очереди) не опрашивается
  `other_active_ai_review_runs` вовсе (только `in_progress`/`queued`). Для
  `cmd_should_run` схема на этой слепоте держится (летящий не видит
  ждущего и публикует вердикт первым). Для `update_branch`,
  `mechanical_rebase.py`, `trigger_ai_review` — узкое окно, где тормоз не
  срабатывает, пока прогон `pending`. Не устранено, только названо.
