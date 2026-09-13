# Tasks: do-pulse-watchdog (#689)

## Backend (pulse_guard/scheduler)

- [x] `pulse_guard.decide_independent_pulse` — чистое решение применимости/
      сигнала (различитель `triggering_actor.login`, порог, нижняя граница
      при пустой выборке независимых тиков).
- [x] `pulse_guard.independent_pulse_check` — IO-обвязка: 1 дешёвый
      `recent_runs` всегда; эскалация через `escalate()`, дедуп парой
      маркеров (`DO_PULSE_MARKER`/`DO_PULSE_RESUMED_MARKER`).
- [x] Вызов из `scheduler.main()` сразу после `heartbeat_check`.
- [x] Тесты (`test_pulse_guard.py`, прод-форма — реальный `gh api
      .../orchestra.yml/runs?event=workflow_dispatch` 2026-09-07): применимость/
      порог/нижняя граница/мутация «порог — одна константа»/эскалация с
      числом часов в тексте/дедуп/закрытие эпизода/холостой ход — 1 вызов.
- [x] Мутационное доказательство (три направленные мутации, каждая красит
      свой тест, снятие возвращает зелёный): подавление `escalate()`;
      снятие проверки `episode_reopened` (дедуп); снятие applicability-гейта
      (`newest_event_age > stale_after_minutes`).
- [x] Проводка в `test_scheduler.py` (все full-main тесты, стабящие
      `heartbeat_check`, дополнены стабом `independent_pulse_check` —
      восемь мест, `replace_all`).

## Документация

- [x] `openspec/changes/do-pulse-watchdog/proposal.md` — граница с #519/#522,
      прод-форма разрыва 2026-09-07, причина в `cf-worker/src/harness.ts::alarm()`
      (названа, не чинится этим change).
- [x] `openspec/changes/do-pulse-watchdog/specs/journal-tasks-hands/spec.md`.

## Вне рамок (см. proposal.md «Что вне рамок»)

- [ ] Починка `cf-worker/src/harness.ts::alarm()` (второй независимый
      будильник вне DO) — отдельная, более дорогая задача, не заводится
      здесь без решения владельца.
