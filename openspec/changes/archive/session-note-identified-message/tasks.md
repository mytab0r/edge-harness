# tasks.md: session-note-identified-message (#794)

- [x] Писатель (`scripts/orchestra/scheduler.py::append_session_notes`)
      кладёт полную форму `data.message` (`id`, `role`, `content`,
      `source.kind`/`provider`/`model`) — не только `id`. Критерий приёмки:
      живой вызов реального пакета `@deepseek-ai/dsh-session@0.1.2-rc.1`
      (`adoptSessionEvent`) принимает построенное событие без исключения.
- [x] `message.id` уникален на событие и в пределах процесса (session_id +
      `GITHUB_RUN_ID` + монотонный счётчик), не зависит от `Date.now`.
      Критерий приёмки: `test_append_session_notes_message_id_unique_per_note_in_one_call`.
- [x] Приёмный роут (`dsh-edge/patches/0004-harness-ingest.patch`,
      `normalizeHarnessIngestEvent`) отказывает громко на пустом/отсутствующем
      `message.id` для `user/message`/`assistant/message`/`tool/result`, и на
      неполной форме (`role`/`source.kind`/`provider`/`model`) для
      `assistant/message`. Критерий приёмки: патч применяется на пине
      апстрима без единого хунка мимо (`git apply` серии 0001-0005 на чистом
      клоне `e1941bb`).
- [x] Гвардия писателя доказана мутацией — `test_scheduler.py`,
      `test_append_session_notes_event_shape_is_allowlisted_assistant_message`.
- [x] Новый инвариант `repo_invariants.py::check_recurring_worker_failure` —
      N подряд прогонов `worker.yml` с одной классифицированной причиной.
      Наблюдательный (не в `CI_GATING`) — зависит от истории прогонов
      workflow, не от диффа PR. Критерий приёмки: `test_repo_invariants.py`,
      `test_recurring_worker_failure_*`.
- [x] Дельта-спека — `specs/journal-tasks-hands/spec.md`.
