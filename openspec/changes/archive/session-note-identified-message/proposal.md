# session-note-identified-message: заметка-итог несёт identified message (#794)

Задача: #794. Дельта-спека:
[specs/journal-tasks-hands/spec.md](specs/journal-tasks-hands/spec.md).
Развилка вариантов —
[design.md](design.md).

## Зачем

`scheduler.py::append_session_notes` (#480, `runner-session-outcome-notes`)
пишет заметку-итог как `assistant/message` с `data.message = {role, content}`
— без `message.id`. Приёмный роут `POST /api/sessions/:id/ingest` (патч
`dsh-edge/patches/0004-harness-ingest.patch`, `normalizeHarnessIngestEvent`)
это принимает: он проверяет только `message`+`content`, а не форму,
которую реально требует хранилище. Ошибка не стреляет на записи — только на
СЛЕДУЮЩЕЙ холодной загрузке той же сессии: `@deepseek-ai/dsh-session`
(пинован в `apps/dsh-edge/package.json` на `0.1.2-rc.1`,
`assertMessageEventShape`) бросает `session event at seq N lacks an
identified message`, оборачиваясь в `stored session "<id>" failed
validation: ...`. `worker.yml` резюмирует сессию `harness-<N>` при повторном
ходе по той же задаче — холодная загрузка испорченной записи валит job.
Живой инцидент: `worker.yml` — 9 прогонов подряд красных на сессии
`harness-257`, 2026-09-08T08:45Z — 2026-09-09T02:08Z (18 часов), конвейер
не поднимает руки. Известные испорченные сессии: `harness-257`,
`harness-215`, `harness-710`.

## Что делается

1. Писатель (`scheduler.py::append_session_notes`) кладёт полное
   `data.message` — не только `id`: реальный пакет (`adoptSessionEvent`,
   проверено живым вызовом на пинованной версии) требует ЕЩЁ `role`,
   `source.kind === "model"` и `source.provider`/`source.model` — id один не
   спасает от того же класса падения, только меняет, на каком поле оно
   случится. `message.id` — составной (`session_id` + `GITHUB_RUN_ID` +
   счётчик процесса), не случайный: тот же приём, что апстрим сам применяет
   для миграции старых сообщений без identity (`legacyMessageId`).
2. Приёмный роут (`normalizeHarnessIngestEvent`, патч 0004) отказывает
   громко на пустом/отсутствующем `message.id` (и, для `assistant/message`
   — на неполной форме `role`/`source`) ДО записи — тот же класс дыры иначе
   открывает любой следующий писатель ingest-шва, а он уже открывался
   ровно так один раз.
3. Новый инвариант `repo_invariants.py::check_recurring_worker_failure` —
   N подряд прогонов `worker.yml` с ОДНОЙ и той же классифицированной
   причиной. Класс, который `failure_watch` (`pulse_guard.py`) не ловит по
   построению (окно свежести 30 минут, дедуп «одна задача на класс»).

## Вне рамок этого change

- Починка уже испорченных сессий `harness-257`/`harness-215`/`harness-710`
  (нужен доступ в морду, живых мутирующих вызовов в прод не делали, см.
  design.md, «Восстановление испорченных сессий»).
- Мисклассификация алертов (провал дозаписи заметки сваливается в разные
  флаги в трёх местах `scheduler.py`) — отдельный класс, отдельная задача.
