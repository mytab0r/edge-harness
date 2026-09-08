# tasks: mechanical-conflict-rebase (#762)

- [x] Модуль `scripts/orchestra/mechanical_rebase.py` — механический git rebase всех
      PR с меткой `conflict` за один проход, без LLM-агента; переиспользует
      `scheduler.conflict_labeled_at` (порядок обхода) и
      `review_labels.other_active_ai_review_runs` (тормоз против ai-review).
- [x] Отдельный workflow `.github/workflows/conflict-mechanical-rebase.yml` (не job
      внутри `orchestra.yml` — изоляция от `pulse_guard.heartbeat_check`, design.md
      «Развилка 2»); тот же триггер, что job `orchestra` (расписание + workflow_dispatch);
      последовательный цикл в одном job'е (обоснование — design.md «Развилка 1»).
- [x] `pulse_guard.WATCHED_WORKFLOWS` — добавлен `conflict-mechanical-rebase.yml`, чтобы
      catastrophic-сбой job'а оставался видимым `failure_watch` тем же механизмом.
- [x] Тесты `scripts/orchestra/test_mechanical_rebase.py` — прод-форма: настоящий git
      (bare-репозиторий, реальный `git rebase`), FakeGh для GitHub API. Сценарий
      «5 PR, 3 сходятся, 2 нет» + мутация (process_pull → всегда "conflict").
- [x] `docs/agents/LABELS.md`, строка `conflict` — упомянуть механический путь как
      первый шаг перед агентским.
- [x] Проверка регрессии: `scripts/orchestra/test_scheduler.py` зелёный без изменений
      (scheduler.py не тронут).
- [x] PR открыт: #764.
