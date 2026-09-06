# Задачи: gate-idempotency

Исполнение — задача #203 (ветка agent/203-worker).

- [x] `scripts/lib/review_labels.py` — `REVIEW_VERDICTS`,
      `verdict_label_changes`, `latest_trusted_comment` (с переводом
      `latest_ai_comment` на общий обход), `comment_update_action`,
      `latest_comment_by_header`, константы
      `CONTRACT_FAIL_HEADER`/`REVIEW_FINDINGS_HEADER`.
- [x] `scripts/orchestra/contract_check.py::fail` — идемпотентный
      комментарий провала (Факт 1), метка ставится как раньше.
- [x] `scripts/review/check_pr.py` — своп вердикт-метки через
      `verdict_label_changes` (Факт 2), находки-комментарий идемпотентно.
- [x] `scripts/review/ai_review.py::cmd_verdict` — тот же класс у `ai:*`:
      своп через `verdict_label_changes(..., AI_VERDICTS)`;
      `apply_large_ok` получает корректное множество меток после свопа.
- [x] Тесты: `scripts/lib/test_review_labels.py` (чистые функции),
      `scripts/orchestra/test_contract_check.py` (Факт 1: same → ни
      POST, ни PATCH; changed → PATCH; first → один POST; чужой текст-близнец
      не глушит), `scripts/review/test_check_pr.py` (Факт 2: same → ни
      одного вызова с метками; change → своп; first → один POST; находки —
      два реальных прогона main(), второй не публикует),
      `scripts/review/test_ai_review.py` (same → нет вызовов с метками,
      change → своп).
- [x] Мутационная проверка (снятие фикса → тест краснеет, восстановление →
      зелёный): Факт 1 — `test_fail_same_violation_posts_no_second_comment`
      (и `test_fail_changed_violation_patches_existing_comment` на форме
      PATCH); Факт 2 — `test_check_pr_unchanged_verdict_touches_no_labels`;
      класс — `test_cmd_verdict_same_verdict_touches_no_labels`.
      Доказано 2026-09-06 прогоном.
- [x] Дельта-спека `openspec/changes/gate-idempotency/specs/journal-tasks-hands/spec.md`
      (п.41.4 — замена якоря таймеров #196/#269).
- [x] `docs/agents/LABELS.md` — строки `contract:failed`, `review:ok`,
      `review:changes-requested`, `ai:failed` приведены к новому поведению.
- [x] Находка ревью #424: идемпотентность 41.1 замораживала таймеры #196
      (`trigger_ai_review`)/#269 (`stale_ready_pulls`) на первой простановке
      вердикта, т.к. они отсчитывали от таймлайн-события `labeled`.
      `scripts/lib/review_labels.py::status_posted_at` — новый якорь, commit
      status `harness/review`/`harness/ai-review` на текущем head (#345,
      публикуется каждым прогоном безусловно). Заодно устранена дупликация
      `last_gate1_labeled_at` между `scheduler.py` и `repo_invariants.py`
      (расходилась дважды — #303, #432) — обе стороны читают
      `review_labels.status_posted_at`. Три доки, утверждавшие старое
      поведение, поправлены: `scheduler.py` (докстринги над
      `last_gate1_labeled_at`/`last_ready_labeled_at`), `pulse_guard.py`
      (`AI_REVIEW_RETRY_AFTER_MINUTES`), `docs/agents/LABELS.md` (`ai:failed`).
- [x] Замер «до»: `scripts/measure/label_churn_203.py` (одноразовый
      диагностический, в git) — 213 labeled/24ч, 88 из них `review:ok`.
- [ ] Замер «после» тем же скриптом по суточному окну после мержа —
      делает следующий прогон/владелец (критерий приёмки 3).
