# tasks: task-state-preconditions (#357)

- [x] `accept_merged_tasks` (scripts/orchestra/scheduler.py) не закрывает
      задачу, если её объявляет ещё один открытый PR — тест с мутацией.
- [x] `scripts/git/task-branch` проверяет существование/открытость/метки
      задачи перед созданием ветки; офлайн — предупреждение, не отказ —
      тест с мутацией (`scripts/git/test/task-branch.test.sh`).
- [x] `scripts/lib/claim_task.py::claim` не выдаёт аренду на закрытую задачу —
      тест с мутацией.
- [x] Четвёртое место (`scripts/worker/task.sh`, `scripts/lib/free_task.py`)
      проверено, находка задокументирована в proposal.md — не исправлено
      (вне рамок этого change).
