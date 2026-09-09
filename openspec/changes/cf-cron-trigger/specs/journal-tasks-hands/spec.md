# Дельта-спека: журнал (journal-tasks-hands) — Cloudflare Cron Trigger, независимый источник тактов (#693)

Секция «Пульс оркестрации»/«Самообновление морды» домовой спеки
(`openspec/specs/journal-tasks-hands.md`) пополняется несколькими
незаархивированными дельтами одновременно (`orchestration-pulse-fail-loud`
пп. 30–33, `do-pulse-watchdog` пп. 42–45, и др.) — номера у них могут
временно пересекаться. Итоговый номер этого пункта присваивается при
архивации, сверяться с доменной спекой на тот момент, не с числом здесь.

## ADDED: Cron Trigger — второй, независимый от alarm() источник тактов

46. Cloudflare Cron Trigger (`cf-worker/wrangler.jsonc` `triggers.crons`,
    интервал 5 минут) дёргает `cf-worker/src/index.ts::scheduled()`, который
    RPC-вызовом дёргает `Harness#scheduledTick()` того же Durable Object, что
    и обычный `fetch()`-путь — не второй объект, не вторая копия логики.
    Такт оркестратора с этого пункта — не только `alarm()` самого DO: сам
    RPC-вызов конструирует DO заново, если он был выгружен из памяти, и тем
    самым доставляет объекту трафик, которого раньше могло не быть
    неограниченно долго (см. п. 46.3).
    46.1. `scheduledTick()` дёргает `workflow_dispatch` (через тот же
        `#dispatchOrchestraTick`, что и `alarm()` — одно место правды, не
        вторая копия `attemptOrchestraDispatch`/`fetchLatestOrchestraRunId`/
        `confirmPreviousRun`/`pulseDetailForRecord`) ТОЛЬКО когда чистая
        функция `pulseStale()` говорит, что здоровый `alarm()` подвис
        (последний успешный тик не обновлялся ≥ 2×
        `HEARTBEAT.selfOrchestrationMs`). Здоровый alarm тикает сам —
        `scheduledTick()` в подавляющем большинстве вызовов не пишет ни
        одной строки (только чтение текущего pulse), не дублирует dispatch
        в ту же минуту.
    46.2. Отказ dispatch'а (`dispatch_ok: false` в последнем пульсе) —
        по-прежнему забота обычного `alarm()` на его собственном тике:
        `pulseStale()` нарочно возвращает `false` для этой ветки, страховка
        Cron Trigger её не подхватывает и не путает зависший alarm с
        отказавшим dispatch'ем.
    46.3. `scheduledTick()` перезакладывает будильник (`await
        this.#ensureHeartbeat()`) ПЕРВОЙ операцией на КАЖДОМ cron-тике,
        независимо от результата п. 46.1 — это чинит класс отказа: если обе
        попытки `ctx.storage.setAlarm` внутри `alarm()` падают (например
        исчерпание суточной квоты `rows_written`, #320), `alarm()` выходит
        БЕЗ нового будильника, а восстановление раньше требовало внешнего
        трафика к объекту (конструктор). Cron — такой трафик, доставляемый
        независимо от активности PR/владельца.
    46.4. Честная граница наблюдаемости: если сам RPC-вызов
        (`env.HARNESS.get(id).scheduledTick()` в `src/index.ts::scheduled()`)
        падает ДО того, как `scheduledTick()` успел записать что-либо в
        pulse — `/api/status` не увидит и этого тика вовсе; единственный
        след — Cloudflare Logs (`wrangler tail`/дашборд), `scheduled()`
        ловит и логирует ошибку, не роняя обработчик. Отказ ПОСЛЕ входа в
        `#dispatchOrchestraTick` виден в `/api/status` тем же
        `status.last_pulse`, что и у `alarm()`.
