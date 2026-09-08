# Инфраструктура Cloudflare: что доступно и что нельзя трогать

Прочитай это до любой работы с Cloudflare API/wrangler в этом репозитории.
Повод: 2026-09-05 система молча упёрлась в суточную квоту DO (`rows_read`,
#320) — причину искали три версии подряд, факт про лимит не был записан.
Собранное здесь — прогон, а не пересказ доков (issue #322). Ссылка на
прогон-доказательство (run, SHA, дата) живёт в одном месте правды —
[docs/research/20-cloudflare-free.md, раздел «API v4: что реально отдаёт
токен репозитория»](../research/20-cloudflare-free.md); здесь она не
дублируется. Классы «прогон, чей SHA недостижим из мерж-хеда (история
переписана после прогона)» и «дубль ссылки в двух файлах расходится при
правке» — оба ловились ревью PR #328 и закрыты самим устройством:
каноническая ссылка в research/20 + правило не переписывать историю ветки
после прогона.

## С чем я работаю

`CLOUDFLARE_API_TOKEN`/`CLOUDFLARE_ACCOUNT_ID` — секреты репозитория, доступны
только в GitHub Actions. Локально их нет и не будет — не пытайся получить их
из окружения агента, любой скрипт из `scripts/cf/` фейлится с явной причиной.

**Аккаунт общий с другими проектами владельца.** В нём 9 воркеров, но этому
репозиторию принадлежат ровно два: `edge-harness` (морда, `cf-worker/`) и
`dsh-edge` (`.github/workflows/deploy-dsh-edge.yml`). Остальные семь — чужие
проекты, их имена **не публикуются** ни в логах, ни в доках этого репозитория
(репозиторий публичный). Добавил или переименовал воркер проекта — обнови
`CF_OWN_WORKERS` в `scripts/cf/lib.sh`: это единственное место правды
allowlist, гвардия (`cf-inventory.guard.sh`) сверяет его с
`cf-worker/wrangler.jsonc` и `deploy-dsh-edge.yml` — расхождение краснеет,
иначе новый свой воркер навсегда останется «чужим, не показано». Инцидент
2026-09-05: первый прогон `cf-inventory`
напечатал чужие имена воркеров в публичный Actions-лог; лог удалён
(`gh api -X DELETE .../actions/runs/{id}/logs`), скрипты переписаны на
фильтрацию по allowlist (`CF_OWN_WORKERS` в `scripts/cf/lib.sh`). Тот же класс
касается DO namespaces, KV, D1, R2, зон — все они account-wide.

## Что реально доступно (таблица, проверено прогоном 2026-09-08, см. каноническую ссылку в шапке)

| Возможность | Статус | Что показывает |
|---|---|---|
| Права токена (`/user/tokens/verify`) | проверено | активен, `status: active` |
| Список воркеров (`/accounts/{account_id}/workers/scripts`) | проверено, отфильтровано | наши: `edge-harness`, `dsh-edge`; ещё 7 в аккаунте — чужие, скрыты |
| Деплой edge-harness (`/accounts/{account_id}/workers/scripts/edge-harness/deployments`) | проверено | полная история деплоев с id/timestamp |
| Bindings edge-harness (`/accounts/{account_id}/workers/scripts/edge-harness/settings`) | проверено | `ASSETS`, `GH_DISPATCH_TOKEN`(secret), `GH_REPO`(plain), `HANDS_TOKEN`(secret), `HARNESS`(DO-класс `Harness`), `SESSION_SECRET`(secret) |
| Bindings dsh-edge (`/accounts/{account_id}/workers/scripts/dsh-edge/settings`) | проверено | `ASSETS`, `DEEPSEEK_API_KEY`/`DEEPSEEK_BASE_URL`/`DEEPSEEK_MODEL`/`DSH_EDGE_ACCESS_KEY`(secrets), `DSH_EDGE_INSTANCE`(DO-класс `DshEdgeInstance`), `GH_RUNNER_REPO`(plain) |
| Поддомен workers.dev (`/accounts/{account_id}/workers/subdomain`) | проверено | `mytab0r` |
| DO namespaces (`/accounts/{account_id}/workers/durable_objects/namespaces`) | проверено, отфильтровано | наши: `edge-harness_Harness`, `dsh-edge_DshEdgeInstance`; 1 чужой в аккаунте, скрыт |
| KV namespaces (`/accounts/{account_id}/storage/kv/namespaces`) | проверено, только счётчик | 7 в аккаунте, 0 у этого проекта (`wrangler.jsonc` без `kv_namespaces`) |
| D1 databases (`/accounts/{account_id}/d1/database`) | проверено, только счётчик | 4 в аккаунте, 0 у этого проекта |
| R2 buckets (`/accounts/{account_id}/r2/buckets`) | проверено, только счётчик | 1 в аккаунте, 0 у этого проекта |
| Зоны/домены (`/zones`, не account-scoped) | проверено | **0 зон во всём аккаунте** — закрывает issue #289: своего домена у CF-аккаунта нет |
| Logpush jobs (`/accounts/{account_id}/logpush/jobs`) | **нет доступа** | HTTP 403 "Authentication error" — токену не хватает права **Logs Read** |
| Живые логи воркера в моменте | доступно вне API | `npx wrangler tail edge-harness` из `cf-worker/` — WebSocket, интерактивно, не для CI |
| Расход Workers requests/сутки | проверено | GraphQL `workersInvocationsAdaptive` тем же токеном: `python scripts/measure/quotas.py` / workflow `quotas.yml` (#324; раздел «Метрики использования» в [research/20](../research/20-cloudflare-free.md), лимит 100k/день в его `LIMITS`) |
| Расход DO rows_read/rows_written | проверено | простым GET не отдаётся — читается через GraphQL Analytics (`POST /client/v4/graphql`) тем же токеном: `python scripts/measure/do_rows_read.py --days N` (задача #320; прогоны 2026-09-05 — раздел «Замер факта: rows_read в проде» в [research/20](../research/20-cloudflare-free.md)) |
| Расход GB-s (duration) | **не подтверждено** | датасетом duration инструмент ещё не снимал; метод — интроспекция схемы, как в `scripts/measure/do_rows_read.py` и `quotas.py` (см. «не подтверждено» в research/20) |
| Статические лимиты Free-плана | см. research | [docs/research/20-cloudflare-free.md](../research/20-cloudflare-free.md) |

**Каких прав не хватает токену** (выдать владельцу, если понадобится):
- **Logs Read** — чтобы видеть, настроен ли Logpush и куда льются логи.

Право **Account Analytics Read** у токена уже есть — иначе не прошли бы живые
замеры `scripts/measure/do_rows_read.py` (GraphQL Analytics тем же
`CLOUDFLARE_API_TOKEN`, прогоны 2026-09-05 в research/20). Вносить его в
«не хватает» — ошибка: агент уйдёт думать, что расход нечитаем, и
переизобретёт готовое (находка ревью PR #328).

## Чем узнать состояние

- `bash scripts/cf/status.sh` — агрегированный инвентарь (workers, деплой,
  bindings-имена, DO/KV/D1/R2/zones счётчики). Только через
  `.github/workflows/cf-inventory.yml` (`workflow_dispatch`) — там секреты.
- `bash scripts/cf/worker-logs.sh` — проверка, настроен ли Logpush (не сами
  логи — их API не отдаёт без Logpush).
- `bash scripts/cf/api.sh <path>` — разовый GET к пути CF API v4 (`<path>` —
  всё после `/client/v4`). Отказ по умолчанию для всего, что не проверка
  токена (`/user/tokens/verify`) и не наш воркер из `CF_OWN_WORKERS`
  (`scripts/cf/lib.sh`) — не блок-лист конкретных известных путей (класс
  инцидента 2026-09-05: незаблокированный account-wide путь печатал бы
  сырьё). Путь вне allowlist — явный opt-in `CF_API_SH_ALLOW_RAW=1`
  (печатает сырьё, не для публичного лога). Тот же контракт: маскирование
  токена, явная причина 403/404, никогда не печатает секрет.
- Все три — только `GET`. Локально без секретов падают с понятной ошибкой
  (`cf_require_env` в `scripts/cf/lib.sh`), не тихо и не с общим "error".

## Границы — чего агент не делает сам

**Разрушительные операции — только владелец**, руками или через уже
существующие ревьюженные пайплайны (`deploy-worker.yml`, `deploy-dsh-edge.yml`):
- удаление воркера, DO namespace, KV/D1/R2-ресурса — необратимо, теряются данные;
- смена маршрутов/доменов (`workers/routes`, зоны) — меняет живой трафик;
- ротация/удаление секретов (`wrangler secret put/delete`) — рвёт авторизацию
  прод-морды мгновенно;
- любой `PUT`/`POST`/`DELETE` к CF API вне этих двух workflow.

`scripts/cf/` **намеренно только `GET`** — ни один скрипт здесь не может
изменить состояние аккаунта, даже по ошибке. Если агенту нужна мутация —
это отдельная задача с ревью, не тихая правка инвентарного скрипта.

## Куда смотреть при отказе

- Скрипт молчит про секреты → `cf_require_env` уже упал первым, читай stderr.
- `НЕТ ДОСТУПА: 403` → см. таблицу выше и раздел "каких прав не хватает";
  выдаёт токену право владелец, не агент.
- `НЕ НАЙДЕНО: 404` → путь неверен или ресурс переименован — не значит "прав нет".
- Нужен факт о поведении CF, которого нет в таблице → добавь прогоном через
  `scripts/cf/api.sh`, задокументируй в [docs/research/20-cloudflare-free.md](../research/20-cloudflare-free.md),
  не гадай.
