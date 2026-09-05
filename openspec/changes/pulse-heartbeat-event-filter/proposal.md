# pulse-heartbeat-event-filter: наблюдатель за пульсом планировщика не видел собственной слепоты (#133)

Задача: #133. Дельта-спека:
[specs/journal-tasks-hands/spec.md](specs/journal-tasks-hands/spec.md).

## Замер (2026-09-05, живой прогон, до правки)

`gh api "repos/mytab0r/edge-harness/actions/workflows/orchestra.yml/runs?event=workflow_dispatch&per_page=10"`
показывает последний тик DO-пульса (`Harness.alarm()`, cf-worker/src/harness.ts)
в **04:35:49Z** — до как минимум **14:24** того же дня ни одного нового
`workflow_dispatch`-прогона `orchestra.yml` нет (~9ч49м при заявленном цикле
15 мин, `HEARTBEAT.selfOrchestrationMs`). Контроль по `schedule`: за то же
окно ровно один прогон, в 13:32:22 — согласуется с ранее измеренной
ненадёжностью GitHub `schedule` на этом репозитории (docs/research/21,
«Замер schedule на этом репозитории», ~6.7% доставки).

Существующий watchdog «кто следит за следящим» (`pulse_guard.heartbeat_check`,
задача #120) не закричал об этом ни разу, хотя порог `HEARTBEAT_MAX_AGE_MINUTES`
(45 мин) был превышен в 20 раз. Причина — в самом watchdog'е, не в пульсе.

## Причина, найденная фактом

`orchestra.yml` несёт два job'а в одном файле:

- `contract` — на КАЖДЫЙ `pull_request` (`opened, synchronize, reopened,
  labeled`), быстрая проверка контракта PR↔задача;
- `orchestra` — сам планировщик (мерж-очередь, предохранитель, ЭТОТ watchdog),
  запускается только на `schedule`/`workflow_dispatch`
  (`if: github.event_name != 'pull_request'`).

`pulse_guard.recent_runs()`/`heartbeat_check()` до этой правки брали
`workflow_runs?per_page=5` БЕЗ фильтра по `event` и искали в них последний
`conclusion: success`. При параллельной работе нескольких агентов
`pull_request`-события (и, значит, зелёные `contract`-прогоны) идут
десятками в час — они и оказывались «последним успехом», маскируя то, что
job `orchestra` не запускался часами. Ровно тот же класс силент-неправды,
что #303 уже закрыл в `fetchLatestOrchestraRunId` (cf-worker/src/harness.ts)
фильтром `event=workflow_dispatch` — только там легитимное событие ровно
одно, а здесь их два (`schedule` И `workflow_dispatch`), поэтому фильтр —
исключение `pull_request`, а не единственное разрешённое значение.

**Подтверждено кодом и тестом:** `test_heartbeat_check_blind_to_pull_request_contract_runs_mutation_guard`
(scripts/orchestra/test_pulse_guard.py) кормится ровно замеренной формой —
россыпь `pull_request`-успехов у самого свежего края и один настоящий
`workflow_dispatch`-успех 9ч49м назад — и доказывает мутацией: без фильтра
`heartbeat_check` молчит (`decide_heartbeat` вернул бы `"ok"` по самому
свежему `pull_request`-прогону, 5 минут возрастом).

**Не подтверждено (см. «Что вне рамок»):** ПОЧЕМУ реальный тик перестал
создавать `workflow_dispatch`-прогон `orchestra.yml` на 9ч49м — это отдельный
вопрос, эта дельта чинит только слепоту наблюдателя.

## Что делается

- `pulse_guard.real_orchestra_ticks(runs)` — чистая функция-фильтр
  (`event != "pull_request"`), тестируется отдельно от IO.
- `heartbeat_check()` применяет фильтр к `recent_runs()` и поднимает
  `per_page` с 5 до 100 (потолок GitHub на страницу): между двумя настоящими
  тиками `orchestra` может быть больше 5 `contract`-прогонов, короткой
  выборки не хватает дотянуться до последнего реального успеха сквозь них.
- Тест на прод-форме данных (реальные таймстампы и id из замера 2026-09-05),
  доказан мутацией (снял фильтр → тест покраснел).

## Что вне рамок

- **Причина самой остановки тика** (403 egress Cloudflare Workers, о
  котором изначально заведена #133, vs что-то ещё) не установлена этой
  дельтой и не может быть установлена без чтения `status.last_pulse.detail`
  cf-worker (`GET /api/status`, требует `HANDS_TOKEN`) или логов Cloudflare
  (`wrangler tail` / дашборд) — ни то ни другое не доступно из этой сессии.
  Владельцу нужно одно из двух: (а) выполнить
  `curl -s -H "Authorization: Bearer $HANDS_TOKEN" https://edge-harness.mytab0r.workers.dev/api/status`
  и поделиться полем `last_pulse` (не токеном), либо (б) открыть в
  Cloudflare дашборде Real-time Logs воркера `edge-harness` за окно
  04:35–14:24 2026-09-05. Эта дельта делает так, что СЛЕДУЮЩИЙ такой провал
  (любой причины) будет виден и в Telegram, и в задаче #120 — не только
  установит причину этого конкретного.
- **Обход «морда dsh-edge → cf-worker → GitHub»**, названный в #133 как
  решение для ИСХОДНОГО замера (`plugins-src/runner-bridge/server/index.js`,
  прямые вызовы `api.github.com` из воркера dsh-edge, измерено 2026-08-31),
  этой дельтой не реализован. Причины: 1) сам факт «блок dsh-edge всё ещё
  жив» не переизмерен (замеру 2026-08-31 почти неделя, блок описан как
  IP/colo-зависимый и мигрирующий — docs/research/30); 2) живой замер этой
  дельты (см. выше) показывает, что dispatch из cf-worker к api.github.com
  (`Harness.alarm()`, тот же API-хост) сам не создавал ранов ~9ч49м —
  ситуация, для которой прокси через cf-worker не помогает по определению:
  если у cf-worker тот же класс проблемы с тем же хостом, маршрутизация
  через него не даёт нового пути наружу. Различить «cf-worker тоже
  заблокирован» от «alarm просто не тикал по другой причине» без
  `status.last_pulse.detail` нельзя (см. пункт выше) — реализовывать прокси
  без этого разбора значит чинить вслепую. Решение по обходу — после того,
  как владелец даст один из двух артефактов выше.
