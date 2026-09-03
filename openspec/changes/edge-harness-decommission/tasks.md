# Задачи: edge-harness-decommission

Исполнение — задача #86 (ветка agent/86-edge-harness-dsh-edge). Порядок —
безопасный относительно живого прода: сначала новый канал (он работает и на
старой морде — ingest-шов #119 в проде), затем чтение и пульс, списание —
последним, за предпосылками.

## Канал конвейера

- [x] `scripts/lib/dsh-edge-pipeline.sh` — механика: логин, begin
      `harness-pipeline`, ingest статуса, ретраи, fail-loud градация FINAL.
- [x] `scripts/plugins/plugin_status.sh` / `integration_status.sh` — тот же
      CLI-контракт, транспорт морды; smoke переписан на заглушку морды.
- [x] `scripts/hands/dsh_task.sh` — текст задачи из payload dispatch;
      жизненный цикл job → сессия конвейера; журнал-seq и heartbeat удалены.
- [x] `scripts/worker/task.sh` — heartbeat-стопгэп удалён.
- [x] `scripts/canary-dispatch.sh` — списан вместе с POST /api/tasks.
- [x] `scripts/lib/test/dsh-clients.smoke.sh` — обновлён (журнала нет).

## Чтение и пульс

- [x] Патч `dsh-edge/patches/0005-harness-pipeline-view.patch`:
      `GET /api/harness/events` в форме журнала над сессией конвейера;
      запись в `dsh-edge/PATCHES.md`; серия 0001–0005 применяется к пину.
- [x] `cf-pulse/` — минимальный инфра-воркер пульса (alarm-цепочка, dispatch
      orchestra, #73 сверка версий с троттлом); решение с тестами
      (`node --test`), health с фактом взведённого будильника.
- [x] `.github/workflows/deploy-pulse.yml` — деплой пульса + гвардия узости
      GH_DISPATCH_TOKEN (переезд из deploy-worker.yml) + синк секрета +
      канарейка «пульс взведён».
- [x] `deploy-dsh-edge.yml` — статусы → канал морды; push-триггер на
      dsh-edge/**/plugins-src/** (иначе правки манифеста ждут ручного
      диспетча).

## Списание

- [x] `.github/workflows/deploy-worker.yml` и `cf-worker/` удалены.
- [x] `.github/workflows/decommission-worker.yml` — предпосылки (нет ссылок
      на журнал, морда жива, канарейка канала) → `wrangler delete`/CF API;
      повтор — зелёный no-op.
- [x] `hands.yml`/`worker.yml` — env журнала вычищен.

## Гвардии и спека

- [x] `scripts/lib/test_dispatch_token_usage.py` — новый pin workflows,
      потребитель узкого токена — deploy-pulse.yml.
- [x] repo-ci: grep-гвардия «HARNESS_URL/HANDS_TOKEN не возвращаются в
      активный код»; тесты решения пульса в CI.
- [x] `openspec/specs/journal-tasks-hands.md` переписана под новый канал
      (журнальные разделы → сессия конвейера + пульс-воркер); дельта — этот
      каталог.

## Пост-мерж (владелец/автоматика)

- [ ] deploy-dsh-edge зелёный на push (морда с патчем 0005), канарейки зелёные.
- [ ] deploy-pulse зелёный, канарейка «пульс взведён» зелёная.
- [ ] decommission-worker зелёный: воркер edge-harness удалён из CF
      (GET несуществующего скрипта → 404), канарейка канала — улика в задаче.
- [ ] Цикл «задача → раннер → статусы в чате» без воркера: runner_task из
      чата поднимает раннер, транскрипт + конвейер-сессия видны в морде.
- [ ] Чистка vars/secrets репозитория: HARNESS_URL, HANDS_TOKEN,
      SESSION_SECRET (использоваться больше не могут — гвардия repo-ci красит
      возврат), старый DO-журнал в дашборде CF — удалить по желанию.
