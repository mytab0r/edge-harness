## Зачем

Задача: #904.

Номера инвариантов в `scripts/orchestra/repo_invariants.py` присваиваются автором PR
вручную — общий числовой ресурс без арбитра, тот же класс, что #1078 у
`docs/decisions/*`/`docs/research/*` (уже починен `scripts/lib/decision_numbering.py`).

Живая коллизия, найденная при подготовке этого PR (2026-09-14, не гипотеза): открытые PR
**#1061** (issue #925) и **#1136** (issue #1121) НЕЗАВИСИМО добавили инвариант **19**
разными функциями (`check_ci_failure_closed_but_main_red` и
`check_continue_on_error_readers`). Этой же коллизии предшествовала ручная перенумерация
17 → 19 (PR #1061, комментарий к ревью) — сам перенос породил новую коллизию, потому что
арбитра не было.

## Решение

Не меняем схему нумерации (сквозная, `int`) — см. `design.md`, раздел «Цена миграции».
Вместо этого — арбитр по образцу `decision_numbering.py`:

- `scripts/lib/invariant_numbering.py` — парсит нумерованный реестр из докстринга
  `repo_invariants.py` (строки вида `N. check_имя_функции`) на `main` и на каждом открытом
  PR (реальный `git show`/`git diff`, без GitHub API для содержимого), находит коллизии
  (`decision_numbering.find_number_collisions`, переиспользован импортом, не
  продублирован).
- `scripts/ci/guards/invariant-numbering-guard.sh` — регистрация в каталоге гвардий
  (#749), без правки `repo-ci.yml`.
- `check_invariant_number_collisions` — обвязка под `check_result.CheckResult`, готовая
  для подключения в `repo_invariants.py::build_report()`, НЕ подключена этим PR — файл
  занят параллельными PR #1136/#1189 (см. design.md).
- Попутно исправлен design-дефект, найденный live в уже слитом `decision_numbering.py`
  (не мой файл, не чиню его этим PR — см. design.md, «Живой дефект»): push в `main` не
  обязан отвечать за коллизию с ЕЩЁ НЕ смёрженным сторонним PR, только за коллизию ВНУТРИ
  самого `main`.

## Затронутые каналы

Только `scripts/lib/`, `scripts/ci/guards/`. `scripts/orchestra/repo_invariants.py` и
`scripts/orchestra/test_repo_invariants.py` НЕ трогаются этим change — заняты
параллельными PR #1136/#1189.
