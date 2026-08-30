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

Авторизация `/api/*` — двумя равноправными способами, токен в URL не ходит:

- **браузер**: `POST /api/session` обменивает `Authorization: Bearer <HANDS_TOKEN>` на
  подписанную сессионную куку `harness_session` (HMAC от `SESSION_SECRET`, HttpOnly,
  SameSite=Strict, Secure, TTL — `SESSION.ttlMs` в `src/config.ts`); дальше все запросы,
  включая WebSocket, авторизуются кукой автоматически. `DELETE /api/session` — выход;
- **job** (`scripts/hands`): `Authorization: Bearer <HANDS_TOKEN>` как раньше.

Запрос с `?token=` отклоняется кодом 400 `query_token_removed` (по образцу dsh-edge,
[`docs/research/11-dsh-edge.md`](../docs/research/11-dsh-edge.md): обмен секрета на
подписанную куку — токен не должен попадать в логи CF и историю браузера).

| Маршрут | Что делает |
|---|---|
| вся таблица | сгенерирована в [`docs/api.md`](../docs/api.md) из `api-spec.json` — руками не править |

## Локальная разработка

```bash
cd cf-worker
npm ci
cp .dev.vars.example .dev.vars   # HANDS_TOKEN=dev-token, SESSION_SECRET=dev-session-secret
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

Секреты воркера (`HANDS_TOKEN`, `GH_DISPATCH_TOKEN`, `SESSION_SECRET`) деплой-воркфлоу
переустанавливает сам при каждом деплое из одноимённых секретов репозитория — вручную
после деплоя их ставить не нужно (воркер мог быть удалён из CF вместе с секретами, шаг
идемпотентен). `SESSION_SECRET` — ключ подписи сессионной куки: вращение разлогинивает
все открытые сессии браузеров.

Плюс переменная репозитория `vars.HARNESS_URL` = публичный URL воркера (для `hands.yml`).

Без `HANDS_TOKEN` API отвечает 401 на всё. Без `GH_DISPATCH_TOKEN` постановка задач
честно отвечает `dispatch: "not_configured"` — «возможности нет» должно отличаться
от «возможность сломана».

## Замеры

Скрипт разового замера «dispatch → первый heartbeat» (задача 5 walking-skeleton)
удалён cleanup'ом 09ef969 вместе с каркасом: метрика считалась по часам DO, а
текущие «руки» слайса dsh-in-job шлют первый heartbeat после минутного
бутстрапа — она перестала измерять очередь GitHub. Хвост распределения снимает
автономная кампания `repository_dispatch → старт job'а` по серверным таймстампам
GitHub: [.github/workflows/dispatch-latency-probe.yml](../.github/workflows/dispatch-latency-probe.yml)
+ [scripts/measure/dispatch_tail.py](../scripts/measure/dispatch_tail.py),
обоснование — [ADR 0005](../docs/decisions/0005-dispatch-tail-campaign.md).
Результаты идут в `docs/research/99-open-questions.md`, сырые данные —
`docs/research/data/dispatch-latency-tail.csv` (ветка `data/dispatch-latency-tail`
до финального мержа).
