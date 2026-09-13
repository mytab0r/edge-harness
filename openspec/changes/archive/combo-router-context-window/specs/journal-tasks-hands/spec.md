# Дельта-спека: окно контекста env-provider (#789)

Правит требование «Маршруты combo-router строятся из
`PLUGINS_SUITE_CANDIDATE_ROUTES`…» из delta-спеки `dsh-in-job`
(«ADDED: Suite ротации учёток combo-router + anthropic-oauth-pool»).

## MODIFIED: Маршрут env-provider — окно контекста из каталога моделей

Было: `contextWindow` маршрута `env-provider` (и записи модели в
`providers.env-provider.models`) заполнялся значением `DSH_MAX_TOKENS`
(потолок ДЛИНЫ ОТВЕТА, adapter-конфиг `llm-deepseek` `maxTokens`) —
смешение двух разных величин отбрасывало главного провайдера
combo-router'ом при контексте, кратно меньшем реального окна модели.

Требование: `contextWindow` маршрута `env-provider` вычисляется функцией
`dsh_model_context_window` (`scripts/lib/dsh-ci.sh`) — поиск `vars.DEEPSEEK_MODEL`
по `id` в `vars.DSH_EDGE_MODEL_CATALOG` (та же переменная и то же значение,
чьё присутствие для `vars.DEEPSEEK_MODEL` уже проверяет `deploy-dsh-edge.yml`
для каталога морды — не второе место правды). Модель, отсутствующая в
каталоге, — фолбэк на `DSH_MAX_TOKENS` с видимым `::warning::`
(«окно контекста модели … не найдено в vars.DSH_EDGE_MODEL_CATALOG»), не
тихая подмена.

Требование: `worker.yml` и `hands.yml` прокидывают `vars.DSH_EDGE_MODEL_CATALOG`
в env шага, вызывающего `dsh_patch_profile` — без этого фолбэк срабатывает
всегда (переменная пуста), поведение осталось бы прежним молча.

Не входит: маршруты-кандидаты ротации учёток
(`PLUGINS_SUITE_CANDIDATE_ROUTES`) уже несут собственные явные значения
`contextWindow` (взяты из примера самого combo-router, не из
`DSH_MAX_TOKENS`) — этим требованием не затронуты. Цепочка провайдеров
(`dsh_run_with_provider_chain`/`ai_dsh.sh`) вызывает `dsh_patch_profile`
только по плоской ветке (`_dsh_patch_profile_plain`), которая поле
`contextWindow` не пишет вовсе, — не затронута.
