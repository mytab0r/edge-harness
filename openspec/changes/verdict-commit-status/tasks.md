# Задачи: verdict-commit-status

Исполнение — задача #345 (ветка agent/345-verdict-commit-status).

- [x] `scripts/lib/review_labels.py` — `STATUS_REVIEW`/`STATUS_AI_REVIEW`,
      `post_commit_status`, `review_status_state`, `ai_status_state`,
      `run_target_url`.
- [x] `scripts/review/check_pr.py` — публикация `harness/review` сразу
      после метки-вердикта, тем же значением `verdict`.
- [x] `scripts/review/ai_review.py::cmd_verdict` — публикация
      `harness/ai-review` сразу после `ai:*`-метки, тем же значением
      `verdict` (`error` → `pending`, обоснование в proposal.md).
- [x] `.github/workflows/pr-review.yml` — `statuses: write`.
- [x] `.github/workflows/ai-review.yml` (job `verdict`) — `statuses: write`.
- [x] Тесты: `scripts/lib/test_review_labels.py` (состояния статусов,
      `post_commit_status`, `run_target_url` — чистые функции без сети),
      `scripts/review/test_check_pr.py`/`test_ai_review.py` (main()/
      cmd_verdict целиком через monkeypatch gh/run_gh, без сети) + гвардии
      по исходнику (публикация идёт через `review_labels`, не второй прямой
      `gh api` рядом с меткой).
- [x] Мутационная проверка: `ai_status_state` без ветки `error → pending`
      красит `test_ai_status_state_error_is_pending_not_failure` и
      `test_cmd_verdict_posts_pending_status_on_transport_error_not_failure`
      (доказано вручную, снятие/восстановление патча).
- [x] Дельта-спека `openspec/changes/verdict-commit-status/specs/journal-tasks-hands/spec.md`.
- [ ] `docs/research/23-platform-native-vs-custom.md` п.2 (PR #344, на
      момент постановки этой задачи ещё не в main) — обновить вывод
      «не берём сейчас» на «реализовано (#345), метки временно остаются» —
      делает тот, кто сливает #344 после этой задачи (или наоборот, кто
      сливает эту после #344), не блокирует эту задачу.
- [ ] Живой прогон на реальном PR: `gh api
      repos/mytab0r/edge-harness/commits/<sha>/status --jq '.statuses[]|
      "\(.context): \(.state)"'` показывает оба контекста (PR того же
      репозитория запускает workflow версией из своего head — правка
      применяется к собственному PR этой задачи без ожидания мержа).
- [ ] Владелец включает контексты в branch protection (команда — в отчёте
      задачи #345) — после подтверждения живым прогоном.
