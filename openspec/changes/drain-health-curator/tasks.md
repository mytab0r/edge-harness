# Tasks: drain-health-curator (#869)

## A. Композитный сигнал drain_gate

- [x] `scheduler.py::drain_gate(repo, now, pulls, merged_pulls, dispatch_allowed, wip_count, conflict_exhausted, contract_failed_count) -> (observations, actions, ok)`
      по образцу wip_gate/conveyor_gate; часы — из max(merged_at) в уже
      полученном all_merged_pulls, без нового сетевого вызова
      (main() передаёт `all_merged_pulls_snapshot`, второй HTTP-вызов не
      заводится).
- [x] Порог DRAIN_STALL_HOURS=4 — константа рядом с WIP_GATE_STUCK_HOURS/
      WORKER_FAILURE_PAUSE_AFTER (одно место правды, см. AGENTS.md).
- [x] Ветка «нечего сливать» — открытых PR-кандидатов к merge_queue нет
      (scheduler.py::merge_queue_candidates) — возвращает ok без тревоги
      (копия критерия приёмки #194).
- [x] Текст эскалации перечисляет ВСЕ верные в этом прогоне причины
      (предохранитель / WIP≥лимита / конфликты с исчерпанным бюджетом /
      contract:failed) — test_drain_gate_escalates_and_names_all_true_causes
      («две причины сразу») и test_drain_gate_honest_about_unknown_cause_when_none_true
      (честный вывод «причина не установлена», не пусто), доказаны мутацией.
- [x] Идемпотентность маркера на WATCHDOG_ISSUE (open/close, тот же приём,
      что WIP_GATE_OPEN_MARKER/CLOSE_MARKER) —
      test_drain_gate_does_not_repost_open_marker_while_episode_active /
      test_drain_gate_closes_episode_when_queue_drains.
- [x] Тест: снятие фикса (закомментировать перечисление причин) красит
      мутационный тест — доказано вручную (снят и восстановлен фикс,
      test_drain_gate_escalates_and_names_all_true_causes покраснел).
- [x] `docs/agents/LABELS.md` — новых GitHub-меток не вводилось (только
      комментарии-маркеры на WATCHDOG_ISSUE, тот же класс, что
      WIP_GATE_OPEN_MARKER, который тоже не в LABELS.md) — нечего вносить.

## B. Автодиспетч pm

- [x] Решить развилку §2.2 design.md — вариант (A), отдельный workflow
      `.github/workflows/pm.yml`, модель — hands.yml (та же DSH headless
      обвязка через `scripts/hands/dsh_task.sh`, не трогается этим change,
      только `TASK_TEXT`); изолирует дорогой LLM-вызов от orchestra.yml, тот
      же принцип, что уже применён к ai-review.yml. Триггер — scheduler.py
      дёргает `gh workflow run pm.yml` (тот же приём, что
      dispatch_conflict_rework уже делает для worker.yml).
- [x] Условие запуска: drain_gate тревога дольше PM_DISPATCH_AFTER_HOURS ИЛИ
      wip_gate stuck-маркер держится дольше WIP_GATE_STUCK_HOURS —
      `scheduler.py::dispatch_pm_groom`,
      test_dispatch_pm_groom_dispatches_when_drain_episode_older_than_threshold /
      test_dispatch_pm_groom_dispatches_when_wip_stuck_marker_present.
- [x] Идемпотентность — один pm-прогон на эпизод (маркер
      PM_GROOM_DISPATCH_MARKER_PREFIX с моментом открытия эпизода-триггера) —
      test_dispatch_pm_groom_is_idempotent_per_episode.
- [x] Механический потолок закрытий (PM_MAX_CLOSURES_PER_RUN) считается по
      факту вызовов gh за прогон (`scripts/orchestra/pm_dispatch.py::
      count_closures_in_window`, по событиям `issues/events`), не по
      декларации модели — красит шаг `pm.yml` при превышении.
- [x] pm не трогает: задачи/PR с исполнителем, `waiting:owner`, `blocked`
      без протухшей ссылки — объявлено словом в TASK_TEXT `pm.yml` (та же
      граница, что pm.md); честная граница design.md §2/«Не подтверждено»
      п.2 — фактическое соблюдение AGENT'ом внутри headless-сессии этим НЕ
      гарантировано, гарантирован только механический потолок закрытий
      (пункт выше), не семантика отбора целей.
- [x] Тест на мутацию: закрытие сверх лимита — тест красный
      (test_check_closures_flags_when_over_budget, доказано снятием фикса).

## C. Карантин задачи-отравы

- [x] Парсер номера задачи из лога worker.yml («Аренда взята: замок
      refs/locks/task-N») — `scheduler.py::worker_lease_task_number`, тот же
      subprocess-контракт (`--allow-escape-sequences`), что
      pulse_guard.last_error_log_line, другая строка.
- [x] `free_task.py` — фильтр карантина реализован НЕ внутри free_task.py
      (риск нехермета тестов: `oldest-free` вызывается task.sh на каждом
      прогоне воркера, а GITHUB_REPOSITORY всегда задан в среде GitHub
      Actions — сетевой вызов внутри CLI сделал бы существующие unit-тесты
      task.sh-контракта недетерминированными/сетевыми), а переиспользованием
      УЖЕ существующего параметра `excluded` этих же функций
      (free_candidates/prioritized_free) со стороны `scheduler.py::
      dispatch_worker` — тот единственный код этого change, что вызывает
      free_task.py НАПРЯМУЮ (в процессе, не через CLI task.sh). Карантинная
      задача, будь она выбрана обычным bare-диспатчем, получает АДРЕСНЫЙ
      dispatch (`inputs[task]=<следующий кандидат>`), минуя task.sh
      собственный независимый пересчёт — тот же класс приёма, что уже несёт
      wip_gate-closed ветка dispatch_worker.
- [x] QUARANTINE_AFTER=3, backoff (QUARANTINE_PROBE_BASE_MINUTES=15/
      QUARANTINE_PROBE_MAX_MINUTES=240) — константы рядом с
      PROBE_BACKOFF_BASE_MINUTES/PROBE_BACKOFF_MAX_MINUTES (форма
      переиспользована, не общая переменная — design.md §3.2).
- [x] Автоматическая проба и автоматическое снятие при успехе; продление
      выдержки при повторном фатальном падении — БЕЗ отдельного маркера
      (design.md §3.2/§3.3): выдержка считается от `created_at` самого
      свежего фатального прогона в потоке уже читаемых worker.yml runs,
      растущий стрик сам продлевает окно.
- [ ] Верхний потолок продлений → эскалация (число — предмет отдельного
      подбора, см. design.md «Не подтверждено» п.3) — НЕ реализовано в этом
      PR: честно оставлено как названный, не решённый вопрос (карантин пока
      может продлеваться неограниченно, упираясь в QUARANTINE_PROBE_MAX_MINUTES
      как потолок КАЖДОЙ отдельной выдержки, не потолок числа продлений).
- [x] Тест: убрать фильтр карантина — задача снова выбирается, тест краснеет
      (test_dispatch_worker_bypasses_quarantined_task_with_addressed_dispatch,
      доказано снятием фикса).
- [x] Закрывает критерий 3 issue #794 — живой повод (задача #140, runs
      34484847840/13:03/12:18) воспроизведён фикстурой
      test_quarantined_task_numbers_flags_repeated_fatal_task_live_case_140 и
      закрывается адресным обходом в dispatch_worker.

## D. Инвариант

- [x] `repo_invariants.py::check_drain_stalled_without_signal` — независимый
      пересчёт (свой обход all_merged_pulls + свой обход открытых PR +
      свой обход комментариев WATCHDOG_ISSUE), не читает внутреннее
      состояние scheduler.py.
- [x] Третий независимый источник — открытые PR-кандидаты к merge_queue, та
      же ветка нормы, что у drain_gate («кандидатов нет — не тревога»,
      находка ревью PR #870, блокирующая): без неё инвариант красил бы
      здоровый простой, который Требование A объявляет нормой.
- [x] Наблюдательный на старте (не в CI_GATING) — замерено на живом
      репозитории (2026-09-10): 32 открытых PR, все кандидаты, последнее
      слияние свежее порога — 0 нарушений; тот же порядок, что у
      инвариантов 8/9/10 (условие обратного включения — повторный замер
      перед правкой CI_GATING, не эта задача).
- [x] Мутационный тест: снять инвариант — фикстура с нарушением перестаёт
      ловиться; фикстура «кандидатов нет, часов много» остаётся зелёной с
      инвариантом включённым.

## E. Документация и увязка с прежними задачами

- [ ] `openspec/specs/journal-tasks-hands.md` — влить дельту при архивации
      (раздел «Конвейер (оркестратор)», рядом с п.16/17) — предмет
      архивации этого change, не этого PR (см. OPENSPEC-PROTOCOL.md).
- [x] Прокомментировать issue #194 фактом реализации (не закрывать самому —
      закрывает тот, кто подтвердит критерий живым прогоном).
- [ ] `docs/agents/ROADMAP.md`/борда — эпик «Конвейер и оркестрация» — не
      затронуто этим PR (роадмап/борда — решение владельца о приоритезации,
      не техническая часть реализации).
