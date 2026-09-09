# combo-router: окно контекста env-provider читается из каталога, не из потолка вывода

Задача: #789. Родитель: [dsh-in-job](../dsh-in-job/proposal.md) — этот change правит
поле `contextWindow` маршрута `env-provider`, добавленного его delta-спекой
(«ADDED: Suite ротации учёток combo-router + anthropic-oauth-pool»).

## Зачем

`scripts/lib/dsh-ci.sh::dsh_patch_profile` заполняет `contextWindow` маршрута
`env-provider` значением `DSH_MAX_TOKENS` (`scripts/lib/dsh-ci.sh:301`,
`131072` — потолок ДЛИНЫ ОТВЕТА модели, adapter-конфиг `llm-deepseek`
`maxTokens`). Combo-router читает это же поле как размер окна контекста
(`compatible()` отбрасывает маршрут при `contextTokens > contextWindow*0.92`).

Живой прогон на фактически сгенерированном конфиге (один маршрут
`env-provider`, `glm-5.3-flash`, реальное окно 1 000 000):

```
маршрутов: 1, contextWindow env-provider = 131072
contextTokens=120586 -> SELECTED env-provider / glm-5.3-flash
contextTokens=157516 -> THROW NO_COMBO_ROUTE : no healthy compatible route
```

При включённой ротации учёток (env-provider + 8 маршрутов-кандидатов,
`PLUGINS_SUITE_CANDIDATE_ROUTES`) потолок сдвигается, но не исчезает —
максимум среди кандидатов 262144:

```
contextTokens=241172 -> SELECTED ollama-cloud-2 | кандидатов: 3
contextTokens=300000 -> THROW NO_COMBO_ROUTE
```

Цена: задача с длинным контекстом раньше уходила к GLM и отрабатывала,
теперь падает — регресс на классе задач, который до этого работал.

## Решение

Настоящее окно контекста модели `vars.DEEPSEEK_MODEL` уже объявлено
переменной репозитория `vars.DSH_EDGE_MODEL_CATALOG` — тем же значением,
чьё присутствие там уже проверяет `deploy-dsh-edge.yml` для каталога морды.
Новая функция `dsh_model_context_window` (`scripts/lib/dsh-ci.sh`) ищет
модель по `id` в этом JSON и возвращает её `contextWindow`; `dsh_patch_profile`
использует результат для маршрута `env-provider` вместо `DSH_MAX_TOKENS`.
Модель вне каталога (запись отсутствует) — консервативный фолбэк на
`DSH_MAX_TOKENS` с видимым `::warning::` — не хуже прежнего поведения (оно
было таким для всех моделей) и не расширяет окно за пределы
неподтверждённого значения.

`worker.yml`/`hands.yml` прокидывают `vars.DSH_EDGE_MODEL_CATALOG` в env шага,
что вызывает `dsh_patch_profile` — третье место (`ai_dsh.sh`/`ai-review.yml`)
suite не использует вовсе (только цепочка провайдеров, `_dsh_patch_profile_plain`,
поле `contextWindow` там не пишется), правка не нужна.

Маршруты-кандидаты ротации (`PLUGINS_SUITE_CANDIDATE_ROUTES`) уже несут
собственные явные значения `contextWindow`, взятые из примера самого
combo-router, — не затронуты, это не то же самое место.

Дельта-спека: [specs/journal-tasks-hands/spec.md](specs/journal-tasks-hands/spec.md).
