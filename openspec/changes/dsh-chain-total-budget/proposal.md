# Суммарный wall-clock бюджет цепочки провайдеров (DSH_CHAIN_TOTAL_BUDGET_SECS)

Задача: #1160 (инцидент-класс #1141).

Design: [design.md](design.md). Спека: [specs/journal-tasks-hands/spec.md](specs/journal-tasks-hands/spec.md).

## Summary

Прогон цепочки провайдеров (`dsh_run_with_provider_chain`, `scripts/lib/dsh-ci.sh`)
получает суммарный wall-clock потолок `DSH_CHAIN_TOTAL_BUDGET_SECS`
(по умолчанию 9000с/150 мин): дедлайн режет нож КАЖДОЙ попытки И КАЖДОГО
ретрая (`min(DSH_TIMEOUT_SECS, дедлайн − сейчас)`, проводка через
`DSH_CHAIN_DEADLINE_EPOCH` в `dsh_run_with_retry`), а паузы ретраев
клампятся тем же дедлайном. Исчерпание — новый терминальный исход цепочки
`chain_budget_exhausted` (fail loud, в логе число опробованных провайдеров
и секунд), задача воркером возвращается в пул СРАЗУ (тем же
`lease_cli release-full`-путём, что соседи по case), без ожидания рипера
или 24-часового TTL.

## Problem / Motivation

- Живые прогоны `worker.yml` 34757182001/34801868104 (2026-09-13/14) держали
  единственный слот воркера ~5 часов каждый: `DSH_TIMEOUT_SECS` ограничивает
  ОДНУ попытку ОДНОГО провайдера и применяется к каждому по отдельности —
  суммарное время перебора цепочки ничем не ограничено, единственные
  ограничители были внешними (рипер 295 мин, таймаут job'а 340 мин), и оба
  трактуют честный перебор как зависание.
- Один прогон, монополизирующий единственный слот часами, блокирует очередь
  задач целиком (факт #1141: 4 часа блокировки 2026-09-13).

## What changes

- `scripts/lib/dsh-ci.sh`: `dsh_run_with_provider_chain` — бюджет, дедлайн
  в epoch, срез на границе провайдера; `dsh_run_with_retry` — нож на каждую
  попытку/ретрай по `DSH_CHAIN_DEADLINE_EPOCH`, fail loud при остатке ≤ 0;
  `dsh_chain_should_advance` знает новый класс отказа.
- Новый failure-reason `chain_budget_exhausted` доведён до всех потребителей
  контракта: `scripts/worker/task.sh` и `scripts/hands/dsh_task.sh`
  (release-full + человеческое сообщение), `scripts/review/ai_review.py`
  (ветка `error_reason`), перечни в `ai_dsh.sh`/`ai-review.yml`.
- Инвариант 21 (`scripts/orchestra/repo_invariants.py::check_worker_run_long_running`,
  наблюдательный, 200 мин) — вторая поверхность того же факта; арифметика
  худшего легитимного прогона живёт ОДНИМ местом в комментарии инварианта.
- Смоук-гвардия `scripts/lib/test/dsh-chain-budget.smoke.sh` (9 сценариев,
  включая исполненные обратные прогоны обоих инцидентов и мутационный
  критерий приёмки), зарегистрирована каталогом
  `scripts/ci/guards/dsh-chain-budget-guard.sh`.
- Рунбук `docs/runbooks/switch-llm-provider.md` описывает бюджет и исход
  `chain_budget_exhausted`.
