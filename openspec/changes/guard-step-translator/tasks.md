# tasks: guard-step-translator (#897)

- [x] `scripts/lib/guard_step_translator.py` — детерминированный перенос
      рукописного шага гвардии из `repo-ci.yml` в `scripts/ci/guards/<имя>.sh`,
      атомарный по вызову, `UnsupportedStepError` на неразобранной форме.
- [x] Самопроверка `_verify_removal` (находка ревью PR #902): структурная
      сверка «старые шаги минус перенесённые» ДО записи файлов на диск —
      закрывает и порчу соседнего шага (пустая строка внутри `run: |`), и
      дублирующееся имя шага. Мутационно доказано: без вызова
      `_verify_removal` `test_translate_repo_ci_raises_when_blank_line_
      inside_run_corrupts_neighbor` и `test_translate_repo_ci_raises_on_
      duplicate_step_name` краснеют.
- [x] `scripts/orchestra/mechanical_rebase.py::migrate_guard_steps_if_needed` —
      подключение между `attempt_rebase("resolved")` и `push_rebased`; ловит
      `UnsupportedStepError` И `yaml.YAMLError`/`OSError` (находка ревью
      PR #902 — докстринг обещает «не может ухудшить исход resolved», а
      незапойманное исключение убивало весь проход `run()`), в обоих
      случаях предупреждение, не крах.
- [x] Газ в `ci_guard_registration_guard.py::check_no_undeclared_step` —
      точное имя файла каталога вместо общей ссылки на #749.
- [x] Гвардия транслятора в `scripts/ci/guards/guard-step-translator.sh`.
- [x] Тесты: `scripts/lib/test_guard_step_translator.py` (13),
      `scripts/lib/test_ci_guard_registration_guard.py` (+2),
      `scripts/orchestra/test_mechanical_rebase.py` (+4).
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
