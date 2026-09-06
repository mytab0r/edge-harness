## Цель
`deploy-dsh-edge.yml` после бампа пина до `dsh-edge-v0.11.1` (#505/PR #513)
снова зелёный: наши клиентские плагины (`plugin-manager`, `integrations`)
собираются на новой версии апстрима.

## Критерий готовности
- Следующий прогон `deploy-dsh-edge.yml` на main зелёный (шаг «Сборка морды
  из исходников» не падает на `assemble-standalone-web.mjs`).
- `curl https://dsh-edge.mytab0r.workers.dev/api/health` отдаёт
  `"version":"0.11.1"` (сейчас всё ещё `0.8.0` — деплой ни разу не прошёл
  на новом пине, прод не пострадал, просто не обновился).
- Разделы «Плагины»/«Интеграции» в браузере открываются без регрессии.

## Площадь
area:worker

## Контекст и ссылки
Живой прогон (красный): https://github.com/mytab0r/edge-harness/actions/runs/34041272592
(job «deploy», шаг «Сборка морды из исходников»), точная ошибка:

```
Error: @edge-harness/dsh-plugin-manager injects missing Edge Web package @deepseek-ai/dsh-client-runtime.
    at main (.../assemble-standalone-web.mjs:218:15)
```

Корень — не патч-серия (та перебазирована и проверена #505: `git apply`
подряд 0001→0005 на чистом клоне 0.11.1 без единого хунка мимо) и не
регресс морды, а НАШИ СОБСТВЕННЫЕ клиентские плагины, которые ссылаются на
пакет апстрима, переставший существовать:

- `plugins-src/plugin-manager/package.json` → `dsh.client.inject`:
  `["@deepseek-ai/dsh-client-runtime", "@deepseek-ai/dsh-client-locale"]`
- `plugins-src/integrations/package.json` → та же запись
- Апстрим убрал `dsh-client-runtime` в 0.10.0 — цитата из его собственного
  changelog (`docs/releases/0.10.0.md` в клоне апстрима): «Upstream baseline
  0.1.2-rc.1: every `@deepseek-ai/dsh-*` package moves from 0.1.1-rc.2 to
  0.1.2-rc.1 (cordis 4.0.2). Upstream removed `dsh-host-apiproxy` and
  `dsh-client-runtime`; the Edge now serves the browser through the upstream
  session, settings, and workspace controllers over the Typert Remote
  protocol.» — это архитектурный демонтаж, не переименование одного пакета.

**Зацепка для замены** (не проверено до конца, требует изучения): апстрим
использует ту же функциональность (регистрация секции настроек, сервис
`slots`) через свой ЖЕ пакет `packages/client/ui-edge` в собственном
монорепо (`src/client/index.ts`): `export const inject = ['slots', 'locale',
'settingsScope']` — без явной ссылки на `dsh-client-runtime` в
`dsh.client.inject` package.json этого пакета вовсе (нужно свериться с самим
package.json апстримного `packages/client/ui-edge`, что именно он там
объявляет — не выяснено в рамках #505). Типы `PropsRuntime`/`InjectFace`
берутся из `@deepseek-ai/dsh-client-ui-slots` — возможно, именно этот пакет
(или что-то ещё из бандла `bootstrap`, см. `assemble-standalone-web.mjs::
bootstrapIds`) теперь предоставляет сервис `slots` вместо `dsh-client-runtime`,
но это предположение, не факт — не проверялось живой сборкой.

Не трогать в этом PR: `dsh-edge/patches/*`, `dsh-edge/upstream.json` — те
уже решены #505.

## Правила
- [x] Я прочитал docs/research/30-rejected-alternatives.md и задача не из отвергнутых
- [x] Критерий готовности проверяем по видимому результату

