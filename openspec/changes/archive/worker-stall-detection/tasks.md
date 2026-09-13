# Tasks: worker-stall-detection (#815)

- [x] `scheduler.py::_run_is_stalled`/`stalled_worker_run` — детектор зависшего
      `in_progress` прогона worker.yml по длительности, порог
      `WORKER_STALL_MINUTES=200` обоснован замером живых длительностей.
- [x] `scheduler.py::worker_runs_active` — зависший `in_progress` не блокирует
      диспатч (единая точка, все три диспетчера получают фикс разом).
- [x] `scheduler.py::reap_stalled_worker_run` — отмена зависшего прогона
      (best-effort) + полное освобождение аренды коррелирующей задачи, шаг
      `main()`.
- [x] `scheduler.py::last_assigned_at` — общий источник признака «когда взяли
      аренду» для `reap_stale` и `reap_stalled_worker_run` (без второго
      обхода таймлайна).
- [x] Тесты: `test_run_is_stalled_true_past_threshold_false_within_threshold`,
      `test_worker_runs_active_treats_ancient_in_progress_run_as_not_blocking`,
      `test_worker_runs_active_still_blocks_recent_in_progress_run`,
      `test_dispatch_worker_dispatches_when_previous_run_is_stalled`,
      `test_reap_stalled_worker_run_noop_when_run_not_stalled`,
      `test_reap_stalled_worker_run_cancels_and_releases_correlated_task`,
      `test_reap_stalled_worker_run_reports_when_no_task_correlates`,
      `test_reap_stalled_worker_run_skips_task_assigned_before_run_started`.
- [x] Обновлены существующие тесты, ставшие зависимыми от wall-clock даты
      фикстур (`assume_worker_not_stalled` helper, стаб
      `reap_stalled_worker_run` в тестах `main()`).
- [x] `scripts/lib/test_pagination_guard.py::ALLOWED_SINGLE_PAGE_CALLS` —
      запись для новой функции `stalled_worker_run`.
- [x] Дельта-спека `specs/journal-tasks-hands/spec.md`.
- [x] `python -m pytest scripts/orchestra/test_scheduler.py scripts/orchestra/test_mechanical_rebase.py scripts/orchestra/test_pulse_guard.py scripts/lib -q` — зелёные.

## Вне рамок

- Подключение heartbeat (`HANDS_TOKEN`/`HARNESS_URL` в окружении
  `orchestra.yml`) как более точного признака зависания — решение владельца.
