# tasks: guard-step-translator (#897)

- [x] `scripts/lib/guard_step_translator.py` — детерминированный перенос
      рукописного шага гвардии из `repo-ci.yml` в `scripts/ci/guards/<имя>.sh`,
      атомарный по вызову, `UnsupportedStepError` на неразобранной форме.
- [x] Самопроверка `_verify_removal` (находка ревью PR #902, второй круг):
      (а) ни одно перенесённое имя не осталось; (б) deep equality — ВЕСЬ
      новый разобранный документ равен «старый документ минус перенесённые
      шаги», не только шаги job `test`. Закрывает порчу соседнего шага
      (пустая строка внутри `run: |`), дублирующееся имя шага и изменения в
      любом другом job'е. Мутационно доказано: без вызова `_verify_removal`
      краснеют `test_translate_repo_ci_raises_when_blank_line_inside_run_
      corrupts_neighbor` и `test_translate_repo_ci_raises_on_duplicate_
      step_name`; без deep-сверки (проверка (б)) краснеет первый из них
      (порча не видна сверке по именам).
- [x] Схлопывание задвоенных пустых строк — только в окрестности удалённых
      диапазонов (второй круг ревью PR #902): содержимое repo-ci.yml вне
      стыков удаления сохраняется байт в байт. Мутационно доказано на коде
      до правки: глобальный проход молча терял пустую строку в heredoc
      чужого job'а (`test_translate_repo_ci_preserves_unrelated_job_with_
      blank_lines_in_heredoc` краснеет).
- [x] `run:` с выражением GitHub Actions `${{ … }}` — громкий
      `UnsupportedStepError` (некритичное замечание ревью PR #902, поднятое
      до отказа: в файле каталога выражение осталось бы дословным текстом,
      shell отдаёт «bad substitution», причём исходный шаг уже удалён —
      чинить негде). Мутационно доказано на коде до правки: шаг мигрирует
      молча (`test_translate_repo_ci_raises_when_run_contains_actions_
      expression` краснеет: DID NOT RAISE).
- [x] `scripts/orchestra/mechanical_rebase.py::migrate_guard_steps_if_needed` —
      подключение между `attempt_rebase("resolved")` и `push_rebased`; ВЕСЬ
      перенос (трансляция + git add + git commit) под одним
      `try/except Exception` (находка ревью PR #902 и её второй круг:
      до правки git-фаза стояла вне try, `GitError` от `git commit`
      превращал "resolved" в "infra-error" без push'а УСПЕШНО
      перебазированной ветки; до первой правки незапойманное исключение
      убивало весь проход `run()`), в любом случае предупреждение, не крах.
      Мутационно доказано: сужение ловушки обратно до частных классов
      краснит `test_migrate_guard_steps_if_needed_returns_warning_when_git_
      commit_fails` (GitError вылетает исключением).
- [x] Газ в `ci_guard_registration_guard.py::check_no_undeclared_step` —
      точное имя файла каталога вместо общей ссылки на #749.
- [x] Шаг, чей `run:` сам вызывает файл каталога (класс обхода (б) из #771,
      находка ревью PR #902, третий круг): стемы живых файлов каталога не
      всегда кончаются на `-guard` (`ci-guard-registration.sh`), поэтому
      проверка коллизии этот класс не ловила — транслятор молча заводил
      обёртку `<имя>-guard.sh` с телом `bash scripts/ci/guards/<файл>.sh`,
      гвардия исполнялась дважды, мутация-критерий #749 не срабатывала.
      Теперь громкий `UnsupportedStepError` по тому же критерию
      `_is_guard_catalog_invocation` (одно место правды), газ гвардии
      регистрации для этого класса советует удалить шаг, а не создать
      обёртку. Мутационно доказано: снятие проверки в translate_repo_ci
      краснит `test_translate_repo_ci_raises_when_step_invokes_existing_
      catalog_file` (DID NOT RAISE), снятие ветки газа краснит
      `test_check_message_advises_deletion_when_step_invokes_catalog_file`
      («создай» появляется, совета удалить нет).
- [x] Гвардия транслятора в `scripts/ci/guards/guard-step-translator.sh`.
- [x] Тесты: `scripts/lib/test_guard_step_translator.py` (16),
      `scripts/lib/test_ci_guard_registration_guard.py` (+3),
      `scripts/orchestra/test_mechanical_rebase.py` (+5).
- [x] Отклонение от текста задачи #897 про «бит исполнения» зафиксировано в
      `proposal.md` (файлы каталога — `100644`, вызов через интерпретатор,
      `exec_bit_guard.py` их не проверяет) — правка кода не требуется, факт
      репозитория уже соответствует поведению транслятора.
- [x] PR открыт: #902.
- [ ] Первый живой прогон `conflict-mechanical-rebase.yml` в проде после
      слияния — наблюдать, что рукописные шаги реально переносятся в
      каталог на живой очереди PR, не только зелёный шаг job'а (AGENTS.md,
      «Проверяй видимый результат, а не шаг»).
- [ ] Дельта-спека `specs/journal-tasks-hands/spec.md` — не заводится этим
      change: контракт `journal-tasks-hands` не меняется, транслятор —
      внутренний механизм конвейера (repo-ci.yml/mechanical_rebase.py), не
      наблюдаемое поведение DO/журнала.
- [ ] Архивация `openspec/changes/guard-step-translator` в `archive/` — после
      слияния PR #902 и живого прогона выше, тем же порядком, что соседние
      change (`docs/agents/OPENSPEC-PROTOCOL.md`, «Кто и чем архивирует»).
