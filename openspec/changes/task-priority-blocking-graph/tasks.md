# Задачи: task-priority-blocking-graph

## 1. Носитель зависимости

- [x] `scripts/lib/task_deps.py` (новый) — `gh_graphql()` (обёртка над
      `gh_call("graphql", ...)`, по умолчанию `_default_gh` = `gh api graphql`,
      инъекция `gh_call` — для scheduler.py, единая точка мока с REST),
      `fetch_pool(repo, label="task")` (пагинированный GraphQL, поля
      number/title/labels/assignees/blockedBy/blocking), `graph_is_empty`,
      `add_dependency`/`remove_dependency` (резолв node id по номеру +
      мутации `addBlockedBy`/`removeBlockedBy`), CLI `pool`/`block`/`unblock`.
- [x] Тест: живой round-trip доказан отдельно (issue #350↔#320, 2026-09-06,
      описан в docstring `task_deps.py` и `proposal.md`) — юнит-тесты
      (`scripts/lib/test_task_deps.py`) кормятся синтетикой через
      monkeypatch `subprocess`, сеть в тестах не участвует.

## 2. Приоритет в free_task.py

- [x] `scripts/lib/free_task.py` — `issue_priority_key`, `prioritized_free`,
      `graph_is_empty`; приоритет по трём уровням (мета-метка →
      `blocking_open` → номер). `oldest_free`/CLI `oldest-free` используют
      новый ключ; старый путь (чистая сортировка по номеру) не остаётся
      живым отдельно — это частный случай нового ключа при пустом графе.
- [x] Тесты: уровень 1 (мета обгоняет прикладную независимо от blocking),
      уровень 2 (внутри уровня — по числу блокируемых), уровень 3 (тайбрейк
      по номеру), смешанный случай (мета с одним блокируемым обгоняет
      прикладную с десятью), пустой граф → предупреждение в stderr,
      мутация (закрытие блокируемых уменьшает счётчик и переворачивает
      порядок). Мутационно доказаны все три уровня (откат каждого красит
      именно свои тесты, не соседние).

## 3. Сборка пула для воркера

- [x] `scripts/worker/task.sh::free_task()` — сборка `issues_file` через
      `task_deps.py pool` (GraphQL) вместо `gh issue list` (REST, не отдаёт
      граф). Контракт вызова `free_task.py oldest-free` не меняется (тот же
      файл на входе, обогащённый полями `labels`/`blocking_open`).
- [x] `scripts/orchestra/scheduler.py::dispatch_worker` — найденный по ходу
      дефект «два места правды»: локальная сортировка по номеру дублировала
      `free_task.py` для строки отчёта «какую задачу возьмёт воркер».
      Заменена на `free_task.prioritized_free` над `task_deps.fetch_pool`
      (тот же источник, что и task.sh); сбой доп. GraphQL-запроса
      деградирует к REST-пулу видимым предупреждением, не падением пульса.
      Тесты: приоритет в отчёте учитывает blocking (не только номер),
      деградация при недоступном графе — оба с мутацией на реальном коде.

## 4. Метка `area:process` и закрытие white-spot в collect_labels.py

- [x] `.github/ISSUE_TEMPLATE/task.yml` — `area:process` пятым значением
      дропдауна «Площадь».
- [x] `scripts/lib/collect_labels.py` — сканирование `options:` дропдауна
      issue-шаблонов (было: только `labels:` фронтматтера — `area:*` были
      невидимы гвардии). Регэксп ограничен строками, целиком совпадающими с
      `_LABEL_TOKEN` (не ловит содержательные Cyrillic-варианты дропдауна
      «Тип» в `white-spot.yml`).
- [x] `docs/agents/LABELS.md` — 5 строк `area:*` (было 0): критерий для
      `area:process` («про то, как ведётся работа: протокол, гвардии,
      контракт, приёмка, аренда, ревью-гейты, CI-ворота — не про
      продуктовую фичу»), краткие строки для worker/hands/docs/orchestra
      (классификация происхождения, не тормоз).
- [x] Проставлена `area:process` на 51 из 104 открытых задач пула по
      названному критерию (живой запрос подтверждает 51, см. PR).

## 5. Документация протокола

- [x] `docs/agents/PROTOCOL.md` — раздел «Зависимости — два механизма»
      расширён третьим (нативный `blockedBy`/`blocking` через
      `task_deps.py block/unblock`, ручной, тот же паттерн что sub-issues) —
      заменяет упоминание «строка «Зависимости» в теле» как актуальную
      практику (текст явно помечает её устаревшей).

## 6. Дельта-спека

- [x] `openspec/changes/task-priority-blocking-graph/specs/journal-tasks-hands/spec.md`
      — готова этим же PR (см. файл рядом).

## Явно не задачи этого change (см. proposal.md Out)

- Workflow, конвертирующий текст issue в связи графа при создании —
  разобрано и отклонено в design.md (развилка б).
- Обратное заполнение графа для существующих ~104 задач.
- Диаграмма Ганта / оценка длительности / метка «просьба заказчика» — #224.
- Исключение заблокированных задач из пула (EXCLUSION, не ORDERING).
