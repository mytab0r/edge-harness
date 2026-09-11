# tasks: orchestra-blind-spots (#887, #888)

- [x] `pulse_guard.WATCHED_WORKFLOWS` — авто-обнаружение из
      `.github/workflows/*.yml` на диске, исключения —
      `WATCHED_WORKFLOWS_EXCLUDED` (имя→причина); тест
      `test_watched_workflows_covers_every_workflow_file` сверяет оба списка
      с каталогом, мутация «вернуть ручной кортеж» красит.
- [x] `.github/workflows/orchestra.yml` — явные `id:` всем continue-on-error
      шагам (`stale_blocked`/`waiting_owner`/`health_audit`); финальный шаг
      «Свод реальных исходов» (`if: always()`,
      `STEPS_JSON: ${{ toJSON(steps) }}`).
- [x] `scripts/orchestra/best_effort_outcome_guard.py` — эскалация реального
      провала (`outcome == failure`) через `escalate_if_new`, тот же канал
      #120+Telegram; провал шага-свода ровно одного класса — сломанная
      проводка (нет `STEPS_JSON`/пустой/не-словарь/не разобранный снимок):
      exit 1 с `::error`, не «💚 провалов не найдено» (находка AI-ревью
      PR #888).
- [x] `scripts/lib/orchestra_workflow_lint.py` — гвардия по исходнику:
      continue-on-error шаг без `id` — нарушение; после последнего
      continue-on-error шага обязан стоять проведённый шаг-свод (run зовёт
      гвардию, `if: always()`, `STEPS_JSON: ${{ toJSON(steps) }}`); шаг-свод
      с `continue-on-error` инвалидирует сам себя; исчезнувший job
      `orchestra` — громкий отказ, не «здоров».
- [x] Регистрация обеих гвардий в каталоге `scripts/ci/guards/` (#749):
      `best-effort-outcome-guard.sh`, `orchestra-workflow-lint-guard.sh`.
- [x] Дайджест долга 4/5/8/10 (`repo_invariants.py`, #888):
      `DEBT_DIGEST_INVARIANTS` + `escalate_if_new`, только при непустом
      долге.
- [x] Тесты: `scripts/orchestra/test_best_effort_outcome_guard.py`,
      `scripts/lib/test_orchestra_workflow_lint.py`,
      `scripts/orchestra/test_pulse_guard.py` (блок авто-обнаружения),
      `scripts/orchestra/test_repo_invariants.py` (дайджест) — прод-форма
      `toJSON(steps)`; мутации приложены в PR #896.
- [x] `docs/agents/LABELS.md`, строка `ci-failure` — множество наблюдаемых
      workflow описано авто-обнаружением, без перечисления имён.
- [ ] Первый живой прогон orchestra после слияния — шаг «Свод реальных
      исходов» зелёный при зелёных гвардиях (пост-мерж проверка proposal).
