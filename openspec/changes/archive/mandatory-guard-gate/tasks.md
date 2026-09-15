# tasks: mandatory-guard-gate (#1280)

## Backend/Git-инфраструктура (единственный раздел — один исполнитель)

- [x] `scripts/ci/run_guards.sh`: `GUARD_CATALOG_SKIP` (опционально, пробел-разделённый
      список имён гвардий), не меняет поведение существующего вызова без переменной.
      Критерий: `python -m pytest scripts/lib/test_ci_guard_registration_guard.py -q`
      зелёный без изменений (46 passed).
- [x] `scripts/lib/pre_push_guard_gate.py`: два режима (`full`/`local`), третье состояние
      через `scripts/lib/check_result.py`, аварийный выход `GUARD_GATE_SKIP_ACK`.
      Критерий: `python -m pytest scripts/lib/test_pre_push_guard_gate.py -q` зелёный
      (11 юнит-тестов: ok/violation/unknown, режим по `GITHUB_ACTIONS`, `GUARD_CATALOG_SKIP`
      реально доходит до дочернего процесса, `main()` блокирует/пропускает по каждому исходу).
- [x] `.githooks/pre-push`: новый хук, зовёт гейт. Критерий: подхватывается существующим
      `core.hooksPath=.githooks` (ставит `scripts/git/task-branch`) без правки самого
      `task-branch`.
- [x] Гвардия каталога `scripts/ci/guards/pre-push-guard-gate-guard.sh` +
      `scripts/git/test/pre-push-guard-gate.test.sh` (поведенческий, реальный `git push`
      на bare origin, 4 случая: зелёная гвардия/красная гвардия/аварийный выход/третье
      состояние). Критерий: `bash scripts/ci/guards/pre-push-guard-gate-guard.sh` зелёный;
      `python scripts/lib/ci_guard_registration_guard.py` не находит нового рукописного шага.
- [x] Доказательство мутацией (issue #1194 формат, прогон вручную — см. отчёт разработчика):
      строка `exec "$python_bin" ...` в `.githooks/pre-push` заменена на `exit 0` —
      `scripts/git/test/pre-push-guard-gate.test.sh` красный (случаи 2/3/4 падают); строка
      возвращена — снова зелёный.
- [x] Обратная проверка на живом случае: `git worktree add --detach <tmp>
      a14deb87ef2f05fd81566310482b57d6cff6218c` + `bash scripts/ci/guards/
      ci-guard-registration.sh` в этом checkout — красный, тест
      `test_live_repo_ci_matches_frozen_allowlist` называет точную причину (новый ручной шаг
      «Тесты уборки мёртвых worktree'ов (#891)» вместо файла каталога). Подтверждает, что
      гейт (режим `local`, эта гвардия не в списке исключений) остановил бы именно этот пуш.
Вне этого change (см. proposal.md, «Не в этом change»): контроль размера диффа
исполнителя заведён отдельной задачей через `scripts/gh/issue-create`, не входит в
чек-лист выше — другая область (`docs/agents/WORKER-PLAYBOOK.md`), не гейт гвардий.
