# Tasks: priority-broken-mechanism-first (#224)

- [x] `scripts/lib/task_deps.py`: `transitive_blocked`/`transitive_blocking_counts`/
      `blockers_of` — чистые функции над уже прочитанным `blocked_by_open`,
      без сети. Тесты: `test_task_deps.py` (глубина цепочки, цикл не вешает,
      обратный BFS до целей). Критерий: `transitive_blocking_counts` не
      меньше прямого `blocking_open` ни на одном кейсе.
- [x] `scripts/lib/free_task.py`: `BROKEN_LABELS`, `_is_broken`,
      `_urgent_numbers`, `issue_priority_key` (4 уровня), `graph_is_empty`
      (обновлён под новые уровни), `priority_reason`, CLI `why <N>
      <issues.json>`. Обратная совместимость: `prioritized_free`/
      `priority_top`/`oldest_free`/CLI `oldest-free` — та же сигнатура для
      вызывающей стороны (`scheduler.py`, `task.sh`, `issue-create`).
- [x] Тесты (`test_free_task.py`): тир 0 (своя метка, транзитивный
      предок сломанного, многошаговая цепочка), `priority_reason` (4
      сценария), CLI `why` (3 сценария), гвардия синхронизации литералов
      `BROKEN_LABELS` с `pulse_guard.py`/`health_audit.py`.
- [x] Доказательство мутацией: убрать `| task_deps.blockers_of(issues,
      broken)` в `_urgent_numbers` → 3 теста красные
      (`test_tier0_propagates_to_transitive_blocker_of_broken_task`,
      `test_tier0_propagates_through_multi_step_chain`,
      `test_priority_reason_names_transitively_blocked_broken_task`) →
      вернуть → зелёные. Выполнено, вывод обоих прогонов записан в PR.
- [x] Замер на живом пуле (303–304 открытых задач, 2026-09-12): очередь
      ДО/ПОСЛЕ, топ-10 в обе стороны, объяснение для 11 задач — см.
      design.md.
- [x] Документация: `docs/agents/LABELS.md` (`area:process`, `ci-failure`,
      `self-audit`), `docs/agents/PROTOCOL.md` (раздел «Нативный граф»),
      `openspec/specs/journal-tasks-hands.md` (п.26) — обновлены под новую
      схему.
- [x] Регресс потребителей: `python -m pytest scripts/lib -q` (700 тестов),
      `scripts/gh/test/issue-create*.test.sh` (3 файла), выборочно
      `scripts/orchestra/test_scheduler.py -k "prioritiz or free_task or
      conflict_rework"` — все зелёные без изменений в их коде.
