# Задачи: runner-sessions-in-dsh-morde

Исполнение — задача #119 (ветка agent/119-runner-sessions-in-dsh-morde).
Порядок ниже отражает фактическую реализацию PR; пост-мерж пункты — остаток.

## Реализовано в PR

- [x] Research: session API dsh-edge выверен живым продом
      ([research/12](../../../docs/research/12-dsh-edge-session-api.md)); вид
      канонических событий снят с npm-пакетов 0.1.1-rc.2 (прод-форма данных).
- [x] Патч `dsh-edge/patches/0004-harness-ingest.patch` (ingest-маршрут) +
      запись в [PATCHES](../../../dsh-edge/PATCHES.md). Проверки: серия
      0001–0003 + 0004 применяется к чистому пину; `tsc --noEmit` без новых
      ошибок; бандл direct/isolated собирается, gzip в бюджете; vitest
      edge-runtime 259 passed (1 платформенный EPERM symlink на Windows);
      локальная интеграция unstable_dev «INGEST-CHECK OK» (батчи,
      перенумерация turn, 400 на чужой тип, replay, blank/title, архив).
- [x] `scripts/lib/dsh-edge-session.sh` + проводка в `task.sh`/`dsh_task.sh`;
      `bash -n` на всё; lib включён в repo-ci.
- [x] Архив после слияния: `scheduler.py after_merge` + env в orchestra.yml.
- [x] Деплой-канарейка ingest-шва в deploy-dsh-edge.yml; env
      DSH_EDGE_URL/DSH_EDGE_ACCESS_KEY в hands.yml/worker.yml (worker остаётся
      disabled — включает владелец), pnpm-шаг воркера.
- [x] Живая проверка нативной части на проде: probe-сессия
      `harness-119-probe` (создана в воркспейсе edge-harness, названа
      «#119: …», заархивирована) — улика в задаче #119.

## Пост-мерж (владелец/оркестратор)

- [ ] Деплой dsh-edge прошёл, канарейка «Канарейка ingest-шва (#119)» зелёная.
- [ ] Добавить `vars.DSH_EDGE_URL` и `secrets.DSH_EDGE_ACCESS_KEY`
      (значение ключа морды) в репозиторий, если ещё не добавлены.
- [x] Визуальная проверка морды: сессия раннера открывается как чат
      (сообщения/think/тулы), после слияния уходит в архив. Владелец увидел
      на демо провайдер/модель-заглушки и «unavailable» в деталях тула —
      разобрано и закрыто структурно задачей #131: демо использовало форму
      protocol-only теста (`dsh-edge/ingest-integration/check.mjs`), не форму
      реального батча раннера; `dsh_edge_verify_transcript`
      (`scripts/lib/dsh-edge-session.sh` + `scripts/lib/verify_transcript.py`)
      теперь проверяет оба инварианта (непустые provider/model, tool-call/
      tool-result корреляция) на КАЖДОМ прогоне worker/hands вместо разового
      ручного просмотра — см. `docs/research/12-…md`, «Не подтверждено».
- [ ] Остаток стрима #112: cf-worker /api/events читает старые
      session_event; write-путь удалён из клиентов. Решение об удалении
      read-пути и дедупликации ingest-батчей (дубли при потерянном ответе)
      — отдельная задача, если владелец сочтёт нужным.
