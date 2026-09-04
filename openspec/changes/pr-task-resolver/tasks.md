# Задачи: pr-task-resolver (#259)

## 1. Резолвер

- [x] `scripts/lib/task_ref.py` — `task_from_branch`, `resolve_pr_task`
      (иерархия: ветка → `closing_issue` необязательный → `declared_tasks`);
      докстринг модуля разделяет «источник задачи» и «упоминания».
- [x] `scripts/lib/test_task_ref.py` — резолвер кормится прод-формой тела
      PR #253 (ветка `agent/227-…`, декларация #227, прозаический #120) и
      PR #282 (dependabot, задачи нет); мутация (возврат к
      `sorted(set(extract_task_refs(...)))[0]`) красит тесты.

## 2. Перевод потребителей с узкой семантикой

- [x] `scripts/review/ai_review.py::task_section` — принимает весь `pull`,
      резолвит номер через `task_ref.resolve_pr_task`, не через
      `sorted(set(extract_task_refs(...)))`.
- [x] `scripts/review/test_ai_review.py` — `task_section` протестирован
      end-to-end (мок `gh`) на прод-форме тела PR #253 и dependabot-PR;
      мутация (откат на `extract_task_refs`) красит тесты.
- [x] `scripts/orchestra/scheduler.py` — проверено: `merged_pr_map`/стадия
      приёмки из задачи не существуют на `main` (часть незамерженного PR
      #253); переводить нечего, пункт закрыт разбором, не изменением кода.
- [x] `scripts/orchestra/contract_check.py` — уже использовал узкую
      `declared_tasks` до этого change; не тронут.

## 3. Гвардия класса «взял не ту функцию»

- [x] `scripts/lib/test_task_ref_usage_guard.py` — статический тест:
      `task_ref.extract_task_refs`/`references_task` вне
      `ALLOWED_WIDE_USAGE` (сейчас только `scheduler.py`) красят CI.
      Мутация (временный вызов широкой функции в `ai_review.py`) доказана.

## 4. Дельта-спека

- [x] `openspec/changes/pr-task-resolver/proposal.md`, `design.md`,
      `specs/journal-tasks-hands/spec.md` (п.17.1).

## Явно не задачи этого change

- Шаблон PR с `Closes #N` и перевод существующих открытых PR на новый
  контракт — отдельная работа (не расширяем scope, см. постановку #259).
- REST-заменитель `closingIssuesReferences` — исследовано, вывод
  отрицательный (design.md): REST `cross-referenced` — то же «упоминание»,
  не формальная связь; отдельной задачи не заводим, вывод зафиксирован
  здесь и в design.md.
