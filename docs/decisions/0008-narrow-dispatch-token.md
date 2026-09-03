# ADR 0008. Узкий GH_DISPATCH_TOKEN: dispatch-токен морды отделён от PAT конвейера

- **Дата:** 2026-08-31
- **Статус:** принято (репо-часть реализована в задаче #6; подмена значения —
  шаг владельца, см. «Миграция»)
- **Смежное:** [research/21 GitHub Actions](../research/21-github-actions.md)
  (токены dispatch, события от GITHUB_TOKEN), [research/20 Cloudflare
  Free](../research/20-cloudflare-free.md) (морда как интернет-поверхность),
  ADR [0005](0005-dispatch-tail-campaign.md) (PAT на всём пишущем пути замера),
  задача #6

## Контекст

В репо-секрете `GH_DISPATCH_TOKEN` лежал широкий личный PAT владельца (vault
`gh_token`): classic-токен со всеми скоупами аккаунта. Его синхронизировали в
секрет воркера — **морды, постоянно смотрящей в интернет**. Утечка значения
(баг воркера, скомпрометированный runner-bridge, любая дыра в API) означала
полный доступ к аккаунту: все репозитории, орги, ключи, удаление репозиториев.

При этом для самого dispatch-пути хватало заведомо меньшего: `POST
/repos/{repo}/dispatches` принимает fine-grained PAT с Contents:write на один
репозиторий. Широким токен стал потому, что на удобное имя молча подсели
остальные потребители: воркер (git push, PR, метки), оркестратор
(update-branch), кампания замера задержки (CSV, PR, комментарии), runner-bridge
морды dsh-edge (issues + workflow dispatch). Сузить значение, не разведя
потребителей, — сломать конвейер: пуш веток и PR остались бы без прав.

## Решение

**Два секрета — две роли. Имя секрета обязано означать его ширину.**

1. **`GH_DISPATCH_TOKEN` (репо + воркер) — узкий fine-grained PAT** только на
   этот репозиторий: **Contents: RW + Actions: RW**. Единственный потребитель —
   `deploy-worker.yml`, синхронизирующий значение в одноимённый секрет ворка
   (`wrangler secret put GH_DISPATCH_TOKEN`). Код морды не меняется:
   `harness.ts` читает тот же `env.GH_DISPATCH_TOKEN`.
2. **`GH_PIPELINE_PAT` (репо) — широкий PAT конвейера** (тот же classic
   `gh_token` из vault). Потребители — всё, чему нужны права шире dispatch:
   `worker.yml` (git/gh агента), `orchestra.yml` (update-branch),
   `deploy-dsh-edge.yml` (синхронизация `GH_RUNNER_TOKEN` runner-bridgeа),
   `dispatch-latency-probe.yml` (диспатч + push CSV + PR + комментарии).
3. **Гвардия класса** — `scripts/lib/test_dispatch_token_usage.py` в CI
   (repo-ci): `secrets.GH_DISPATCH_TOKEN` вне deploy-worker.yml — красный
   прогон; деплой обязан сохранять синхронизацию; бывшие широкие потребители
   обязаны читать `GH_PIPELINE_PAT`. Доказана мутацией: каждая из трёх правил
   красится своей правкой.
4. **Гвардия узости значения** — шаг deploy-worker.yml, включаемый переменной
   `vars.GH_DISPATCH_TOKEN_KIND=fine-grained`: GET `/repos/{repo}` под
   проверяемым токеном; классический PAT Отличается заголовком
   `X-OAuth-Scopes` (у fine-grained его нет) — его наличие = красный деплой.

### Почему Actions:write, а не только Contents:write (поправка к задаче)

Формулировка задачи — «для repository_dispatch достаточно Contents:write» —
верна для `POST /repos/{repo}/dispatches` и неверна для секрета целиком: пульс
морды тем же токеном зовёт **workflow_dispatch** оркестратора
(`actions/workflows/orchestra/dispatches`) и деплоя dsh-edge — а это
**Actions: write** (research/21). Токен уже в этом не участвует: issues, PR,
метки, runner-bridge — всё уехало на `GH_PIPELINE_PAT`.

## Почему широкий PAT не исчез совсем

- **События от `GITHUB_TOKEN` не зажигают проверки** (research/21): пуш ветки
  PR, update-branch, создание PR под `github.token` оставили бы CI спящим.
- **Операции шире одного пермишена**: оркестратору и воркеру нужны
  contents + pull-requests + issues + actions разом.
- Замена широкого classic PAT на «четырёхпермишенный fine-grained» — отдельное
  решение владельца без выигрыша для этой задачи: класс атак на конвейер тот
  же (job'ы GitHub Actions с секретами), а ограничение fine-grained на пуши
  коммитов, меняющих `.github/workflows` — не подтверждено (воркер такие ветки
  толкает, напр. #137).

## Миграция (порядок важен)

1. **До мержа** (сделано воркером): секрет `GH_PIPELINE_PAT` установлен
   значением широкого PAT — мерж развода потребителей ничего не ломает: оба
   имени указывают на одно старое значение.
2. **Владелец** (единственный шаг, который нельзя делегировать — PAT создаются
   только в UI): fine-grained PAT «только этот репозиторий, Contents RW +
   Actions RW» → `gh secret set GH_DISPATCH_TOKEN` (значение не должно
   транзитом проходить через публичные поверхности — issue, PR, логи).
3. **Деплой** (workflow_dispatch deploy-worker или следующий пуш в
   `cf-worker/**`): синхронизирует узкое значение в секрет воркера.
4. **Владелец**: `gh variable set GH_DISPATCH_TOKEN_KIND --body fine-grained` —
   армирует гвардию узости; с этого момента возврат классического PAT в секрет
   красит деплой.
5. **Проверка видимым результатом**: `scripts/canary-dispatch.sh` — POST
   /api/tasks → `dispatched:true` → появился run `hands` (repository_dispatch).
   Критерий готовности задачи #6 — прогон этой канарейки после шагов 2–3.

## Последствия

- Морда держит токен, худший сценарий утечки которого — создание workflow-run
  в этом репозитории. Класс «широкий PAT в интернет-поверхности» закрыт **для
  dispatch-пути морды** — и только для него: воркер dsh-edge с runner-bridgeом
  по-прежнему получает широкий PAT (`GH_RUNNER_TOKEN` из `GH_PIPELINE_PAT`);
  его сужение — отдельная задача (Issues, файл ревью #146).
- Право на имя: новое использование `GH_DISPATCH_TOKEN` вне deploy-пути —
  гвардируемый redflag, а не стиль.
- Ограничение гвардии узости (зафиксировано честно): GitHub не интроспектирует
  пермишены fine-grained токена — проверяется тип токена (нет X-OAuth-Scopes) и
  читаемость репозитория, но не набор прав. Полная уверенность — в рунбуке
  создания токена (шаг 2 миграции).
