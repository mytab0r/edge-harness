# Tasks

- [x] Источник окна контекста — `dsh_model_context_window` читает
      `vars.DSH_EDGE_MODEL_CATALOG` по id модели, фолбэк на `DSH_MAX_TOKENS`
      с видимым `::warning::` для моделей вне каталога.
      Критерий: `scripts/lib/dsh-ci.sh::dsh_patch_profile` подставляет
      результат в `contextWindow` маршрута `env-provider` (providers и
      routes), не `DSH_MAX_TOKENS`.
- [x] Проводка `vars.DSH_EDGE_MODEL_CATALOG` в `worker.yml`/`hands.yml` —
      шаги, вызывающие `dsh_patch_profile` с активной suite.
      Критерий: env-переменная присутствует в обоих шагах.
- [x] Прочёс класса «потолок вывода в поле окна контекста» по репозиторию.
      Критерий: второе место не найдено (см. proposal.md) — grep по
      `DSH_MAX_TOKENS`/`contextWindow` не даёт новых совпадений класса.
- [x] Тест-гвардия `scripts/lib/test/dsh-context-window.guard.sh`, доказана
      мутацией (снял фикс — красный, вернул — зелёный).
      Критерий: зарегистрирована шагом в `repo-ci.yml` (текущая конвенция
      каталога `scripts/lib/test/*.guard.sh` — `scripts/ci/guards/` из
      #749/#771 на момент этой задачи ещё не влит в main).
