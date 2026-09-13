# merge-reaction-registry: единый реестр реакций на мерж + дедуп по head_sha + инвариант/гвардия класса

Задача: #955. Следствие #929. Код: `scripts/lib/merge_reactions.py`
(`react_to_merge`), `config/merge-reactions.json`, `scripts/orchestra/
scheduler.py::after_merge`, инвариант в `scripts/orchestra/
repo_invariants.py::check_merge_reaction_gaps`, гвардия класса
`scripts/lib/merge_reactions_registry_guard.py`. Дельта-спека:
[specs/journal-tasks-hands/spec.md](specs/journal-tasks-hands/spec.md).

## Зачем

Установленный факт (не гипотеза): мерж PR под `GITHUB_TOKEN` не порождает
событие `push` — постоянное свойство GitHub (защита от рекурсии), не
регрессия. Доказано `timeline` (`actor.login` события `merged`): PR #868
слил человек (admin-мерж) → push-прогон `repo-ci.yml` стартовал через 2
секунды; PR #872/#878 слил `github-actions[bot]` (оркестратор) → ни одного
push-прогона. Пока часть слияний делали руками, дыра была не видна; как
только слияния стали полностью автономными, `repo-ci.yml`/`codeql.yml`/
`worker-ci.yml` и деплои перестали запускаться после мержа вовсе.

Раньше существовало две независимых копии диспатча (`dispatch_deploy_on_
merge` для `cf-worker/`/`dsh-edge/`) без дедупа и без покрытия
`repo-ci.yml`/`codeql.yml`/`worker-ci.yml` — та же болезнь, что #218
(«встроенный `GITHUB_TOKEN` не порождает проверок»), четвёртым экземпляром.

## Что делается

- **Реестр** `config/merge-reactions.json` — декларативная таблица
  «путь-префикс → workflow, реагирующий на push по main», плюс раздел
  `excluded` для workflow, сознательно не участвующих (пример:
  `plugin-forge.yml` — диспатч требует динамических входов, которые
  статическая запись `prefix→workflow` не выражает, остаётся задачей #946).
- **Единая функция диспатча** `react_to_merge` (заменяет обе прежние копии):
  перед диспатчем каждого workflow спрашивает Actions API, есть ли уже
  прогон на этот `merge_commit_sha` — дедуп закрывает живой класс
  дублирования (push + dispatch на один и тот же коммит, #929). Каждый
  workflow реестра обрабатывается независимо: сбой сети на одном не
  отменяет диспатч остальных.
- **Инвариант** `check_merge_reaction_gaps` (номер занят на месте архивации —
  см. `scripts/orchestra/repo_invariants.py`, докстринг модуля): слияние в
  окне [GRACE; WINDOW] минут назад, затронувшее путь реестра, без прогона на
  свой `head_sha` — нарушение с фактом (workflow, короткий sha, возраст).
  Кандидат, который не удалось проверить (сеть/квота), помечается отдельно
  как «unchecked» и не читается как «нарушений нет».
- **Гвардия класса** `merge_reactions_registry_guard.py`: любой
  `.github/workflows/*.yml` с `on.push`, покрывающим `main` (в любой из трёх
  валидных форм Actions — dict/список/строка), не зарегистрированный в
  реестре (ни как реакция, ни как `excluded` с причиной), красит CI.

## Отвергнутые варианты

Не значится в `docs/research/30-rejected-alternatives.md`.
