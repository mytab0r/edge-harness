# tasks: ci-guard-catalog (#749)

## Механизм

- [x] `scripts/ci/run_guards.sh` — перебор `scripts/ci/guards/*.sh`,
  падение одной гвардии красит весь прогон, пустой каталог красит явно.
- [x] `.github/workflows/repo-ci.yml`, job `test`: один шаг «Каталог
  гвардий scripts/ci/guards — перебор», `env: GH_TOKEN` на уровне ЭТОГО
  шага (не job — ревью PR #771, minor 6: job-level расширял бы радиус до
  всех 95 шагов без единого потребителя внутри каталога).
- [x] `scripts/ci/test/run-guards.smoke.sh` — прогон реального
  `run_guards.sh` на синтетическом каталоге, включая мутации (удаление
  файла, файл вне соглашения `*.sh`, файл-пустышка, пустой каталог — все
  красят CI явно).

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

- [x] `scripts/lib/ci_guard_registration_guard.py` + `ALLOWLIST` (77
  оставшихся рукописных имён шагов — методика замера см. `design.md`,
  «Как несутся `if:`/`env:`»; детекция по имени ИЛИ по содержимому `run:`,
  ревью PR #771 блокирующая 2) + `ALLOWLIST_RATCHET_MAX` (только вниз,
  major 5) + `INFRA_EXEMPT_STEP_NAMES` (major 5).
- [x] `scripts/lib/test_ci_guard_registration_guard.py` — мутация тремя
  сторонами (новый рукописный шаг / устаревшая запись ALLOWLIST / рост
  ALLOWLIST сверх ratchet), включая сценарий двойного прогона (гвардия и в
  каталоге, и рукописным шагом вне соглашения именования).

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
  PR; из 14 — 7 несут `repo-ci.yml` как один из конфликтующих файлов, у
  четырёх из них — #328/#571/#596/#640 — `repo-ci.yml` ЕДИНСТВЕННЫЙ
  конфликтующий файл, см. правку ревью PR #771 major 11).
- [x] `docs/agents/INFRA-GH.md` — строка про каталог `scripts/ci/guards/`.

## Остаток (сознательно НЕ в этом change)

Миграция оставшихся 77 рукописных шагов `job test` (записи `ALLOWLIST`
`scripts/lib/ci_guard_registration_guard.py`, методика замера —
`design.md`) в `scripts/ci/guards/*.sh` — предмет ОТДЕЛЬНОЙ задачи, **#774**
(заведена со ссылкой на #749, ревью PR #771 major 5), не пункт настоящего
`tasks.md`: issue #749 прямо требует закрывать эту задачу «по механизму +
одной перенесённой гвардии», а не по «перенесены все 77» (раздел «Оговорка
про миграцию»). Порядок для будущей миграции — гвардии вне открытых PR
первыми (см. `design.md`, «Миграция остатка»).
