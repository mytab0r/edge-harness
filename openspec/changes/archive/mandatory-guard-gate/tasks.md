# tasks: mandatory-guard-gate (#1280)

## Backend/Git-инфраструктура (единственный раздел — один исполнитель)

- [x] `scripts/ci/run_guards.sh`: `GUARD_CATALOG_SKIP` (опционально, пробел-разделённый
      список имён гвардий), не меняет поведение существующего вызова без переменной.
      Критерий: `python -m pytest scripts/lib/test_ci_guard_registration_guard.py -q`
      зелёный без изменений (46 passed).
- [x] `scripts/lib/pre_push_guard_gate.py`: два режима (`full`/`local`), третье состояние
      через `scripts/lib/check_result.py`, аварийный выход `GUARD_GATE_SKIP_ACK`.
      Критерий: `python -m pytest scripts/lib/test_pre_push_guard_gate.py -q` зелёный
      (18 юнит-тестов после доводки ai-ревью PR #1285: ok/violation/unknown, режим по
      `GITHUB_ACTIONS`, `GUARD_CATALOG_SKIP` реально доходит до дочернего процесса в local
      и изымается из окружения в full, префлайт `gh auth status` → unknown при
      gh отсутствующем/неаутентифицированном, потолок времени прогона → unknown,
      `main()` блокирует/пропускает по каждому исходу).
- [x] Замер стоимости шага на реальном каталоге (требование задачи; доводка ai-ревью
      PR #1285, числа и решение — в proposal.md, «Замер стоимости шага»): full 81 c
      в CI (шаг «Каталог гвардий», run 34909630823 на head этого PR) / 76 c суммой по
      гвардиям на Linux-раннере воркера; local ~72 c (скип двух гвардий экономит 2,3+2,2 c,
      ~6% — причина скипа ловушка #1228, не цена). Решение по числам: full без отбора,
      порог пересмотра — 600 c.
- [x] `.githooks/pre-push`: новый хук, зовёт гейт. Критерий: подхватывается существующим
      `core.hooksPath=.githooks` (ставит `scripts/git/task-branch`) без правки самого
      `task-branch`.
- [x] Гвардия каталога `scripts/ci/guards/pre-push-guard-gate-guard.sh` +
      `scripts/git/test/pre-push-guard-gate.test.sh` (поведенческий, реальный `git push`
      на bare origin, 5 случаев после доводки ai-ревью PR #1285: зелёная гвардия/красная
      гвардия/аварийный выход/третье состояние/gh недоступен → третье состояние с фактом
      про gh). Критерий: `bash scripts/ci/guards/pre-push-guard-gate-guard.sh` зелёный;
      `python scripts/lib/ci_guard_registration_guard.py` не находит нового рукописного шага.
- [x] Доказательство мутацией (issue #1194 формат, прогон вручную — см. отчёт разработчика;
      перепрогнан на финальном хеде доводки): строка `exec "$python_bin" ...` в
      `.githooks/pre-push` заменена на `exit 0` —
      `scripts/git/test/pre-push-guard-gate.test.sh` красный, exit 1: случай 1 зелёный,
      случаи 2/4/5 прошли пуш, который обязан был быть отклонён, случай 3 потерял
      предупреждение аварийного выхода; строка возвращена — все пять случаев зелёные, exit 0.
- [x] Обратная проверка на живом случае: `git worktree add --detach <tmp>
      a14deb87ef2f05fd81566310482b57d6cff6218c` + `bash scripts/ci/guards/
      ci-guard-registration.sh` в этом checkout — красный, тест
      `test_live_repo_ci_matches_frozen_allowlist` называет точную причину (новый ручной шаг
      «Тесты уборки мёртвых worktree'ов (#891)» вместо файла каталога). Подтверждает, что
      гейт (режим `local`, эта гвардия не в списке исключений) остановил бы именно этот пуш.
Вне этого change (см. proposal.md, «Не в этом change»): контроль размера диффа
исполнителя заведён отдельной задачей через `scripts/gh/issue-create`, не входит в
чек-лист выше — другая область (`docs/agents/WORKER-PLAYBOOK.md`), не гейт гвардий.
