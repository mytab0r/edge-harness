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
- [x] Живой прогон на реальном PR (#346): `pr-review.yml` чекаутит
      доверенный код из `ref: main` (trust-zone, см. шапку workflow) — новый
      `check_pr.py` появится на `main` только после мержа этого PR, поэтому
      CI самого PR #346 отработал СТАРЫМ `check_pr.py` (без статуса). Живой
      прогон НОВОГО кода против реального PR доказан прямым запуском
      продакшен-функций локально с реальным `gh`/токеном (не мок):
      `check_pr.py --pr 346` (полностью, без подмен) поставил
      `harness/review: success`; `review_labels.post_commit_status` вызван
      напрямую с контекстом `harness/ai-review` (честно `pending` —
      настоящего AI-ревью новым кодом ещё не было, не выдуманный вердикт).
      `gh api repos/mytab0r/edge-harness/commits/aa1e193.../status --jq
      '.statuses[]|"\(.context): \(.state)"'` →
      `harness/review: success`, `harness/ai-review: pending`.
- [ ] Владелец включает контексты в branch protection (команда — в отчёте
      задачи #345) — после подтверждения живым прогоном.

## Доработка по ревью PR #346 (вердикт rework → исправлено)

- [x] [blocker] Skip-путь второго гейта (`check_pr.py::ai_verdict_keep` ==
      True) оставлял `harness/ai-review` без статуса на новом head:
      `ai-review.yml` сам эту ветку не проходит (`should_run_ai_review`
      отдаёт `go=false`), значит статус там ставить некому — после
      включения required status checks PR застревал бы в «Expected»
      навсегда. Исправлено: `check_pr.py` зеркалит уже вынесенный вердикт
      на текущий head через `post_commit_status`, читая `reviewer:` из уже
      прочитанного (для сверки отпечатка) AI-комментария — второго
      сетевого запроса и второго решения нет. Тесты:
      `test_check_pr_mirrors_ai_status_on_keep_path`,
      `test_check_pr_mirrors_ai_status_rework_on_keep_path`,
      `test_check_pr_mutation_guard_no_mirror_without_keep`
      (`scripts/review/test_check_pr.py`); мутация подтверждена вручную —
      снятие зеркала красит первые два теста.
- [x] [minor] `post_commit_status` докстринг «140 байт» → «140 символов»
      (срез `[:140]` режет по символам Python-строки, лимит API — в
      символах; байтовая формулировка соблазняла бы будущий фикс резать по
      байтам UTF-8 и обрезать кириллицу сильнее нужного).
- [x] [minor] `proposal.md`: ссылка на «ADR 0012» помечена форвард-ссылкой
      явно (как и research/23 выше) — номер 0012 уже занят в main другим
      решением (`docs/decisions/0012-orchestra-event-trigger-merge-loop.md`),
      PR #340 предлагает тот же номер для «Merge Queue недоступна» —
      коллизия резолвится при слиянии #340, здесь номер не фиксируется.
- [x] [мелочь] Дельта занимала п.19 (уже «Аутентификация» в основной
      спеке) — перенумеровано в свободный п.38 (следующий за максимумом
      37 в `openspec/specs/journal-tasks-hands.md` на момент правки).
