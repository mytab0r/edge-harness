# harness-pulse — минимальный инфра-воркер пульса конвейера

Задача #86 (миграция с edge-harness воркера, дизайн —
[`openspec/changes/edge-harness-decommission/design.md`](../openspec/changes/edge-harness-decommission/design.md)).
Принял на себя alarm-пульс списываемого воркера `cf-worker`, ничего больше:
ни журнала, ни очереди, ни UI, ни публичной поверхности (кроме health).

## Что делает

Один DO (`Pulse`, SQLite) с alarm-цепочкой: конструктор закладывает первый тик,
`alarm()` перезакладывает следующий **до** любой работы — падение сети не может
оборвать цепочку. На каждом тике (15 минут):

1. `workflow_dispatch orchestra.yml` — пульс оркестрации. GitHub cron на репо
   не тикает/деградирует (замеры #73 и #269), без пульса конвейер стоит.
2. Самообновление морды (#73): `GET /api/health` морды против
   `registry.npmjs.org/dsh-edge/latest`; расхождение при истёкшем троттле
   (4 ч, ключ в storage DO) → `workflow_dispatch deploy-dsh-edge.yml`. Решение —
   чистая функция [`src/decision.ts`](src/decision.ts), та же, что была в
   cf-worker; тесты — `npm test` (node --test, ноль зависимостей).

## Конфигурация

| Имя | Откуда | Смысл |
|---|---|---|
| `GH_DISPATCH_TOKEN` | секрет воркера; синхронизирует deploy-pulse.yml из секрета репозитория | узкий fine-grained PAT (Contents+Actions на этот репозиторий, ADR 0008) |
| `GH_REPO` | переменная воркера; ставит deploy-pulse.yml из `github.repository` | репозиторий диспетча (в коде не зашит — тест гвардит) |

## health и канарейка деплоя

`GET /api/health` прокидывается в DO и возвращает `next_alarm_at` — канарейка
деплоя (`deploy-pulse.yml`) требует именно **взведённый будильник**, а не HTTP
200: деплой, не оставивший живого пульса, — не деплой, а поломка (fail loud).
Тот же запрос создаёт DO и запускает конструктор — первый тик закладывается.

## Деплой

`.github/workflows/deploy-pulse.yml`: гвардия узости GH_DISPATCH_TOKEN
(переезжала из deploy-worker.yml без изменений логики), `wrangler deploy`,
синк секрета, канарейка health. Локально: `npx wrangler deploy` в этом каталоге.
