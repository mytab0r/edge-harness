# cf-worker: морда и мозг

<!-- heartbeat push: cf-worker/** -->

Воркер Cloudflare: статика морды (Workers Assets) + один Durable Object `Harness`
(SQLite) с журналом, очередью задач и heartbeat'ом рук. Схема и лимиты — в
[`docs/research/20-cloudflare-free.md`](../docs/research/20-cloudflare-free.md),
дизайн скелета — в [`openspec/changes/walking-skeleton/design.md`](../openspec/changes/walking-skeleton/design.md).

## API

Спека — [`api-spec.json`](api-spec.json): единственное объявление маршрутов. Из неё
строятся серверный роутинг, клиентская таблица (`public/assets/config.js`), документация
([`docs/api.md`](../docs/api.md), генерация `npm run docs`) и проверки-канарейки
(`test/api-contract.spec.ts`, `scripts/canary-ui.mjs`). Правила контракта —
[ADR 0004](../docs/decisions/0004-api-contract.md).

Все `/api/*` требуют `Authorization: Bearer <HANDS_TOKEN>` (WebSocket — `?token=` в query,
потому что браузерный WS заголовки ставить не может).

| Маршрут | Что делает |
|---|---|
| вся таблица | сгенерирована в [`docs/api.md`](../docs/api.md) из `api-spec.json` — руками не править |

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
