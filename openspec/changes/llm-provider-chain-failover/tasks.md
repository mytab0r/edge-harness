# Задачи — цепочка LLM-провайдеров (#727)

## Механизм (lib, один раз — используется всеми каналами)

- [x] `dsh_run_with_provider_chain`/`dsh_chain_should_advance`/`dsh_extract_reset_hint`
      в `scripts/lib/dsh-ci.sh` — единственное место правды, вызывается по
      имени, не копируется.
- [x] `vars.DSH_PROVIDER_CHAIN` — формат и обоснование (design.md), значение
      выставлено живым `gh variable set` (GLM + NVIDIA).
- [x] Гвардия класса #153 (`provider-default.guard.sh`) зелёная на диффе —
      проверено прогоном, литералов вне allowlist нет.
- [x] Смоук-гвардия `scripts/lib/test/dsh-provider-chain.smoke.sh` — реальное
      исполнение `dsh_run_with_provider_chain` (не bash -n), доказано мутацией
      (`dsh_chain_should_advance` → всегда `return 1`, тест 1 краснеет).

## Гейт 2 — ai-review.yml (сделано этим PR)

- [x] `ai_dsh.sh` вызывает `dsh_require_provider_chain`/
      `dsh_run_with_provider_chain` вместо `dsh_require_provider_env`/
      `dsh_run_with_retry` напрямую.
- [x] `ai-review.yml` передаёт `DEEPSEEK_API_KEY`+`NVIDIA_API_KEY` (оба
      секрета) и `vars.DSH_PROVIDER_CHAIN`, пробрасывает `chain_provider`/
      `chain_reset_hint` в job `verdict`.
- [x] `ai_review.py::error_reason`/`build_comment`/`cmd_verdict` — новое
      значение `all_providers_exhausted`, факты `provider:`/`reset-at:` в
      шапке комментария.
- [x] `scheduler.py::trigger_ai_review` (#196) — не дёргает авто-повтор
      вслепую при известной дате сброса в будущем, эскалирует вместо этого
      (issue #120 + Telegram), идемпотентно на эпизод; газ автоматический
      (дата наступила → авто-повтор снова идёт как обычно).
- [x] Тесты: `test_ai_review.py` (error_reason/build_comment, прод-форма
      RATE_LIMIT из run 34176910458), `test_scheduler.py` (hold-back/
      идемпотентность/газ по дате, прод-форма `parse_reset_hint_dates`).

## Осталось (не в этом PR — область явно сужена, proposal.md «Область»)

- [ ] `worker.yml`/`scripts/worker/task.sh` — та же цепочка вместо
      `dsh_require_provider_env`/`dsh_run_with_retry`; порядок относительно
      монтажа плагина `dsh-hands-streamer` требует проверки живым прогоном
      (см. proposal.md, «Область»).
- [ ] `hands.yml`/`scripts/hands/dsh_task.sh` — то же самое; дополнительно
      затронут bootstrap-event журнала (`$DSH_MODEL`/`$DSH_MAX_TOKENS` в
      теле события) — модель на момент bootstrap известна только как
      `chain[0]`, если чейн переключится, событие будет называть не тот
      провайдер, который в итоге отработал; решить, приемлемо ли это или
      нужен апдейт события после факта.
- [ ] `scripts/lib/test/dsh-clients.smoke.sh` — расширить фикстуры под
      `vars.DSH_PROVIDER_CHAIN` для worker/hands сценариев (сегодня они всё
      ещё используют одиночный `DEEPSEEK_BASE_URL`/`DEEPSEEK_MODEL`/
      `DEEPSEEK_API_KEY` — не трогать без завершения пункта выше).
