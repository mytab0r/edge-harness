# cf-worker: морда и мозг

Воркер Cloudflare: статика морды (Workers Assets) + один Durable Object `Harness`
(SQLite) с журналом, очередью задач и heartbeat'ом рук. Схема и лимиты — в
[`docs/research/20-cloudflare-free.md`](../docs/research/20-cloudflare-free.md),
дизайн скелета — в [`openspec/changes/walking-skeleton/design.md`](../openspec/changes/walking-skeleton/design.md).

## API

Все `/api/*` требуют `Authorization: Bearer <HANDS_TOKEN>` (WebSocket — `?token=` в query,
потому что браузерный WS заголовки ставить не может).

| Маршрут | Что делает |
|---|---|
| `GET /api/status` | руки живы/нет (порог `HEARTBEAT_FRESH_MS`), счётчики задач |
| `POST /api/events` | батч событий `{task_id, events:[{seq, kind, data}]}`; идемпотентность по `UNIQUE(task_id, seq)` |
| `GET /api/events?after=&limit=&task_id=` | replay журнала, заголовки `x-has-more` / `x-next-after` |
| `GET /api/events.live?after=` | WebSocket downlink-only (гибернация); клиент, пишущий в сокет, получает `1008` |
| `POST /api/tasks` | задача в очередь + `repository_dispatch`; без `GH_DISPATCH_TOKEN` честно отвечает `not_configured` |
| `GET /api/tasks`, `GET /api/tasks/:id` | очередь и задача; в задаче — замер `latency_ms` «dispatch → первый heartbeat» |
| `POST /api/heartbeat` | отметка живости рук; первая отметка задачи фиксирует `latency_ms` |

## Локальная разработка

```bash
cd cf-worker
npm ci
cp .dev.vars.example .dev.vars   # HANDS_TOKEN=dev-token
npm run types                    # после изменений в wrangler.jsonc
npm test                         # vitest на настоящем workerd
npm run dev -- --port 8808       # 8787 бывает в запрещённом диапазоне Windows
node scripts/smoke-local.mjs http://127.0.0.1:8808
```

Smoke — то, что vitest-петля проверить не может: доставку broadcast'а живому сокету
(см. [`docs/research/22-cf-testing-toolchain.md`](../docs/research/22-cf-testing-toolchain.md)).

## Деплой

Автоматический — `.github/workflows/deploy-worker.yml` при изменениях в `cf-worker/**`
на main. Требует секретов репозитория `CLOUDFLARE_API_TOKEN` и `CLOUDFLARE_ACCOUNT_ID`.

Разовые настройки после первого деплоя (секреты воркера переживают деплои):

```bash
npx wrangler secret put HANDS_TOKEN         # тот же токен, что в GitHub secret HANDS_TOKEN
npx wrangler secret put GH_DISPATCH_TOKEN   # GitHub PAT с Contents:write на этот репозиторий
```

И переменную репозитория `vars.HARNESS_URL` = публичный URL воркера (для `hands.yml`).

Без `HANDS_TOKEN` API отвечает 401 на всё. Без `GH_DISPATCH_TOKEN` постановка задач
честно отвечает `dispatch: "not_configured"` — «возможности нет» должно отличаться
от «возможность сломана».

## Замеры (задача 5 walking-skeleton)

```bash
HARNESS_URL=https://… HANDS_TOKEN=… python scripts/measure/dispatch_latency.py --n 20
```

Результаты идут в `docs/research/99-open-questions.md`.
