# Tasks: worker-two-slots (#827)

- [x] `.github/workflows/worker.yml` — вход `slot` (default `"1"`), `run-name`
      несёт номер слота, `concurrency.group` зависит от входа
      (`worker-${{ inputs.slot }}`).
- [x] `scheduler.py::WORKER_MAX_CONCURRENCY = 2` — константа с обоснованием
      (замер GH Actions лимитов + честное «данных по NVIDIA RPM нет,
      консервативно N=2»).
- [x] `scheduler.py::active_worker_runs`/`free_worker_slot` — единый источник
      занятости слотов и выбора свободного.
- [x] `scheduler.py::dispatch_worker` (обе точки dispatch) и
      `dispatch_conflict_rework` (одна точка dispatch) — переведены на
      `free_worker_slot`, передают `-f inputs[slot]=N`.
- [x] `scheduler.py::worker_runs_active` — семантика сохранена («хотя бы один
      активен»), используется `mechanical_rebase.py` и веткой эскалации
      `dispatch_conflict_rework` без изменений.
- [x] `scheduler.py::stalled_worker_runs`/`reap_stalled_worker_run` —
      обобщены на несколько одновременно зависших прогонов (было — только
      первый в списке).
- [x] Тесты: `free_worker_slot` (3 сценария), dispatch в свободный слот
      (worker + conflict-rework, по 2 сценария), оба слота заняты (3
      сценария, переименованы из «воркер активен»), два зависших прогона
      одновременно.
- [x] `scripts/lib/test_pagination_guard.py::ALLOWED_SINGLE_PAGE_CALLS` —
      записи для `stalled_worker_runs`/`active_worker_runs` (замена записей
      `worker_runs_active`/`stalled_worker_run`, переставших делать сырой
      `gh()`-вызов).
- [x] Дельта-спека `specs/journal-tasks-hands/spec.md`.
- [x] `python -m pytest scripts/orchestra/test_scheduler.py scripts/orchestra/test_mechanical_rebase.py scripts/lib -q` — зелёные.
- [x] Мутация: `WORKER_MAX_CONCURRENCY = 1` красит тесты на свободный слот;
      `stalled_worker_runs` обрезан до первого — красит тест на два зависших
      прогона одновременно.

## Вне рамок

- Поднятие N выше 2 — требует измеренного лимита RPM/квоты NVIDIA.
- Правки `task.sh`/`claim_task.py` — атомарность лизы уже не зависела от
  числа воркеров.
