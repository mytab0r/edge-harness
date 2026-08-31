# narrow-dispatch-token: морда держит узкий dispatch-токен, конвейер — свой PAT

Задача: #6. Проблема: в репо-секрете `GH_DISPATCH_TOKEN` жил широкий личный PAT
владельца (vault `gh_token`, classic со всеми скоупами аккаунта), и деплой
синхронизировал его в секрет воркера — интернет-поверхности, у которой
худший сценарий утечки был «полный доступ к аккаунту GitHub». На удобное имя
подсели и широкие потребители (воркер, оркестратор, кампания замера,
runner-bridge), поэтому просто сузить значение значило сломать конвейер.

Решение и ход миграции — [ADR 0008](../../docs/decisions/0008-narrow-dispatch-token.md);
проверенные факты о токенах dispatch — [research/21](../../docs/research/21-github-actions.md).

## Что меняется

1. Репо-секрет `GH_DISPATCH_TOKEN` — теперь **узкий fine-grained PAT**
   (Contents RW + Actions RW, только этот репозиторий). Единственный
   потребитель — `deploy-worker.yml` (синхронизация в секрет воркера).
   Код морды не меняется: `harness.ts` читает тот же `env.GH_DISPATCH_TOKEN`.
   Actions RW нужен потому, что пульс морды зовёт workflow_dispatch
   оркестратора и деплоя dsh-edge — «достаточно Contents:write» из задачи
   верно только для `POST /repos/{repo}/dispatches` (поправка внесена в
   research/21 и ADR).
2. Новый репо-секрет `GH_PIPELINE_PAT` — широкий PAT конвейера (значение —
   прежний `gh_token`; застейджен воркером до мержа). Потребители:
   `worker.yml`, `orchestra.yml`, `deploy-dsh-edge.yml` (синк `GH_RUNNER_TOKEN`),
   `dispatch-latency-probe.yml`.
3. Гвардия класса: `scripts/lib/test_dispatch_token_usage.py` в repo-ci —
   `secrets.GH_DISPATCH_TOKEN` вне deploy-worker.yml красит CI; мутациями
   доказано, что каждое из трёх правил ловит свою поломку.
4. Гвардия узости значения: deploy-worker.yml проверяет токен (GET
   `/repos/{repo}`; `X-OAuth-Scopes` в ответе = classic PAT = красный деплой).
   Включается переменной `vars.GH_DISPATCH_TOKEN_KIND=fine-grained` одновременно
   с подменой значения — до того гвардия лишь предупреждает.
5. Канарейка видимого результата: `scripts/canary-dispatch.sh` — POST
   /api/tasks → `dispatched:true` → появился run `hands` (repository_dispatch).

## Что НЕ меняется

- Поведение API морды: те же маршруты, тот же ответ `dispatch:
  "not_configured"` без токена (спека `journal-tasks-hands.md` п.12).
- Значение широкого PAT (он же `gh_token` в vault) — меняется только имя
  секрета, под которым его читают воркфлоу.

## Границы (честно)

- Подмену значения `GH_DISPATCH_TOKEN` на узкий токен делает только владелец:
  PAT создаются исключительно в UI GitHub, а значение не должно транзитом
  проходить через публичные поверхности. Рунбук — в комментарии задачи #6.
- Пермишены fine-grained токена GitHub не интроспектирует: гвардия проверяет
  тип токена и читаемость репозитория, но не набор прав.

## Критерий готовности задачи

POST /api/tasks создаёт dispatch с узким токеном (канарейка из п.5 после шагов
владельца и деплоя); широкий PAT в `GH_DISPATCH_TOKEN` больше не живёт ни в
репо-секрете, ни в воркере (он остаётся только как `GH_PIPELINE_PAT` —
конвейеру без него не зажигать CI, см. ADR «Почему широкий PAT не исчез»).
