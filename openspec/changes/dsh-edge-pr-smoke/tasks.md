# Задачи: dsh-edge-pr-smoke

Один раздел — задача целиком в одной ветке/PR (issue #600), без разделения
на Frontend/Backend.

## CI/Workflow

- [x] `dsh-edge/e2e-smoke/browser-walk.mjs` — обход вкладок Settings + сбор
      находок консоли/сети, вынесен из `smoke.mjs` в общий модуль.
- [x] `dsh-edge/e2e-smoke/smoke.mjs` — тонкая обёртка над `browser-walk.mjs`
      (прод, поведение не изменилось).
- [x] `dsh-edge/e2e-smoke/pr-check.mjs` — поднимает собранный артефакт
      локально через `unstable_dev` (морда + заглушка журнала), гоняет
      `browser-walk.mjs` против локального адреса.
- [x] `.github/workflows/dsh-edge-pr-smoke.yml` — триггер `pull_request` по
      `dsh-edge/**`, сборка артефакта (без Cloudflare-шагов) + `pr-check.mjs`.
- [x] `scripts/lib/test_dsh_edge_pnpm_guard.py` — новый workflow добавлен в
      `PNPM_TREE_WORKFLOWS` (класс #43 расширен на третий workflow).
- [x] `docs/decisions/0017-dsh-edge-pr-smoke-local-worker.md` + запись в
      `docs/INDEX.md`.
- [x] `openspec/specs/journal-tasks-hands.md` — дельта-спека сложена в базу
      одновременно с реализацией.

## Проверка

- [x] Локальная сборка артефакта (клон на пине, патч-серия, sha256 плагинов)
      прогнана вручную до открытия PR.
- [ ] Мутационная проверка: намеренная коллизия неймспейса локали (issue
      #518/#547) красит `pr-check.mjs` с внятным сообщением; откат к чистой
      сборке зеленит обратно.
- [ ] Зелёный прогон `dsh-edge-pr-smoke.yml` на самом PR (доказательство —
      сам факт зелёного CI).
