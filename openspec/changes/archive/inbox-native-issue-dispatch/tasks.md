# Задачи: инбокс без нового секрета (#20)

- [x] `#dispatchIssueCreation` (было `#createIssue`): `repository_dispatch`
      (`event_type: inbox-issue`) под `GH_DISPATCH_TOKEN` вместо прямого
      вызова Issues API под `GH_ISSUES_TOKEN`.
- [x] Новый job `.github/workflows/inbox-issue.yml`: создаёт issue штатным
      `github.token` (`permissions: issues: write`), callback в DO.
- [x] Новый маршрут `POST /api/messages/issue-created`: замыкает петлю
      204 → факт создания issue; CAS по `claimed_ts`.
- [x] `GH_ISSUES_TOKEN` убран отовсюду (harness.ts, типы окружения,
      deploy-worker.yml, .dev.vars.example, wrangler.jsonc).
- [x] `scripts/lib/test_dispatch_token_usage.py::EXPECTED_WORKFLOWS`
      расширена на `inbox-issue.yml`.
- [x] Дельта-спека `specs/journal-tasks-hands/spec.md` (п. 34), правка
      базовой спеки `openspec/specs/journal-tasks-hands.md`.
- [x] ADR 0011 помечен ЗАМЕНЁННЫМ; новый ADR 0015.
- [x] Тесты (vitest, cf-worker): dispatch+confirm, CAS по `claimed_ts`,
      явный отказ job'а, маскирование секретов в `client_payload`, 4xx/5xx
      на dispatch. Мутационная проверка: снят early-return на 204 (тест
      красится: `done` вместо `processing`), снят `claimed_ts` из CAS
      (тест красится: `accepted: true` вместо `false`).
- [x] PR #362 закрыт без слияния (ADR 0015 «Последствия»; действие вне кода
      этой дельты — GitHub).
- [x] Остаток закрытой #20 (issue-след директив) оформлен новой узкой
      задачей #389 — это этот PR. Переоткрытие самой #20 невозможно:
      «закрытая задача не переоткрывается никогда» (AGENTS.md, решение
      владельца 2026-09-06), пульс оркестратора
      (`scheduler.py::reject_reopened_tasks`) вернул бы её обратно.
