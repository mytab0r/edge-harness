# tasks: ci-guard-catalog (#749)

## Механизм

- [x] `scripts/ci/run_guards.sh` — перебор `scripts/ci/guards/*.sh`,
  падение одной гвардии красит весь прогон, пустой каталог красит явно.
- [x] `.github/workflows/repo-ci.yml`, job `test`: `env: GH_TOKEN` на
  уровне job, один шаг «Каталог гвардий scripts/ci/guards — перебор».
- [x] `scripts/ci/test/run-guards.smoke.sh` — прогон реального
  `run_guards.sh` на синтетическом каталоге, включая мутацию (удаление
  файла убирает проверку из прогона).

## Миграция-доказательство

- [x] `scripts/ci/guards/stale-blocked-guard.sh` — перенесена гвардия
  протухшей метки `blocked` (#334), старый рукописный шаг убран из
  `repo-ci.yml`.
- [x] `scripts/ci/guards/ci-guard-registration.sh` — добавлена без единой
  правки `repo-ci.yml` (криterion приёмки, коммит проверен `git show
  --stat`).
- [x] `scripts/ci/guards/run-guards-mechanism.sh` — добавлена без единой
  правки `repo-ci.yml` (второе доказательство).

## Гвардия на рецидив

- [x] `scripts/lib/ci_guard_registration_guard.py` + `ALLOWLIST` (67
  оставшихся рукописных имён шагов).
- [x] `scripts/lib/test_ci_guard_registration_guard.py` — мутация обеими
  сторонами (новый рукописный шаг / устаревшая запись ALLOWLIST).

## Совместимость с orphan_test_guard

- [x] `scripts/lib/orphan_test_guard.py::iter_guard_catalog_steps` +
  `_catalog_runner_is_wired`.
- [x] `scripts/lib/test_orphan_test_guard.py` — новые тесты, включая
  мутацию «тест виден только через каталог».
- [x] Живой прогон `python scripts/lib/orphan_test_guard.py` — 0 сирот
  после миграции.

## Документация

- [x] `design.md` — форма, несение `if:`/`env:`, решение по миграции,
  замер `git merge-tree` (14/17 конфликтов — baseline, 0 новых от этого
  PR).
- [x] `docs/agents/INFRA-GH.md` — строка про каталог `scripts/ci/guards/`.

## Остаток (сознательно НЕ в этом change)

Миграция оставшихся 66 рукописных шагов `job test` в
`scripts/ci/guards/*.sh` — предмет ОТДЕЛЬНОЙ задачи (заводится со ссылкой
на #749 после мержа этого PR), не пункт настоящего `tasks.md`: issue #749
прямо требует закрывать эту задачу «по механизму + одной перенесённой
гвардии», а не по «перенесены все 66» (раздел «Оговорка про миграцию»).
Порядок для будущей миграции — гвардии вне открытых PR первыми (см.
`design.md`, «Миграция остатка»).
