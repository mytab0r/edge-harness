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
- [x] Проверка регрессии: `scripts/orchestra/test_scheduler.py` зелёный (при
      изначальном merge этого change scheduler.py не тронут; итерация #1032 ниже
      вернулась в файл осознанно — см. её пункт).
- [x] PR открыт: #764.
- [x] Итерация #1032, рычаг 1 — гейт воркера сужен с repo-wide до per-task:
      `mechanical_rebase.worker_blocks_pr` (queued — безусловно; in_progress
      младше `sch.WORKER_CLAIM_TRACE_GRACE_MINUTES` без CLAIM_VIA-следа —
      консервативно; след чужой задачи — пропуск), общий фетч сведён в
      `sch.active_worker_runs` (булев `worker_runs_active` — к нему же),
      `run_age_minutes` стал публичным без второй копии арифметики. Мутации
      исполнены: снятое сужение и снятый грейс-порог красят свои тесты.
- [x] Итерация #1032, рычаг 2 — `scripts/orchestra/additive_conflict_merge.py`:
      механическое сведение класса «обе стороны независимо дописали элемент в
      одну точку реестра» (критерий безопасности, отказ всей пачки, верификация
      ast.parse + pytest ДО `git rebase --continue`); подключён каталогом гвардий
      #749 (`scripts/ci/guards/additive-conflict-merge-guard.sh`), workflow
      ставит pytest; dry-run против 12 реальных конфликтных PR — дословно в теле
      PR #1033 (честный 0/12 с причиной по каждому).
- [x] Дельта-спека и design.md несут оба рычага итерации #1032 (гейт —
      «Гонка с агентским путём» пересмотрен замером; аддитивное сведение —
      требование + сценарии в spec.md, развилка в design.md; находка ai-review
      PR #1033: спека противоречила коду).
- [ ] Первый живой прогон `conflict-mechanical-rebase.yml` в проде после слияния —
      наблюдать, что PR с меткой `conflict` реально сходятся (или содержательный
      конфликт остаётся `conflict` и падает агенту), не только зелёный шаг job'а
      (AGENTS.md, «Проверяй видимый результат, а не шаг»).
- [ ] Дельта-спека `specs/journal-tasks-hands/spec.md` — перенос в
      `openspec/specs/journal-tasks-hands.md` (OPENSPEC-PROTOCOL.md, «Что происходит с
      дельта-спекой после завершения»): редакторская правка после того, как поведение
      подтверждено живым прогоном выше, не выполняется этим PR.
- [ ] Архивация `openspec/changes/mechanical-conflict-rebase` в `archive/` — наступает
      после слияния этого PR (быстрый путь инварианта 4 сработает на смёрженном
      main), не в ветке PR: `git mv` в ветке ломается гвардией свежести
      `.githooks/pre-commit` (нет `refs/remotes/origin/main` в CI-чекауте автофикса —
      см. заведённую задачу про archive-fixup ниже).
