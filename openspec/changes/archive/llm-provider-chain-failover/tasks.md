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

## Гейт 3 — worker.yml (#797, сделано этим PR)

- [x] `scripts/worker/task.sh` вызывает `dsh_require_provider_chain`/
      `dsh_run_with_provider_chain` вместо `dsh_require_provider_env`/
      `dsh_run_with_retry`. Порядок относительно монтажа плагина
      `dsh-hands-streamer`: профиль затравлен ПЕРВЫМ провайдером цепочки
      (chain[0], тот же `dsh_patch_profile`, не второй механизм) ДО первого
      `dsh` этого прогона (`dsh plugin add`) — `initProfile` видит уже
      существующий `cordis.patch.yml` и не перезаписывает его дефолтом;
      цепочка на шаге прогона перепатчивает профиль заново на каждую
      попытку (полная перезапись файла патча) — монтаж плагина (отдельный
      слой `dsh.profile.bundles`, независимый от `cordis.patch.yml` —
      research/10-dsh-architecture.md, «Порядок слоёв») этим не
      затрагивается. Подтверждено рассуждением по research-доку и ручной
      проверкой `dsh_patch_profile` (дважды подряд разными провайдерами —
      оба раза корректный файл), НЕ живым прогоном реального `dsh`-бинарника
      (недоступен в среде разработки этого PR) — живой прогон worker.yml
      после мержа остаётся закрывающей проверкой этого пункта.
- [x] `worker.yml` передаёт `NVIDIA_API_KEY`+`DSH_PROVIDER_CHAIN`;
      `DEEPSEEK_BASE_URL`/`DEEPSEEK_MODEL` убраны из статичного `env:` шага
      (их теперь выставляет сам `task.sh` из цепочки — тот же контракт, что
      уже несёт `ai-review.yml`).
- [x] `all_providers_exhausted` в `task.sh` обрабатывается тем же путём, что
      `quota_exhausted`/`rate_limit_retry_budget_exceeded` (release-full,
      честное сообщение в задачу и Telegram, не «воркер не справился»).
- [x] `scripts/lib/test/dsh-clients.smoke.sh` — worker-сценарии переведены на
      `vars.DSH_PROVIDER_CHAIN` (общая фикстура с ai-review-сценариями этого
      же файла); прогон смоука в среде разработки этого PR (Windows,
      git-bash) упирается в независимую от диффа проблему — `claim_task.py`
      (python, нативный Windows-интерпретатор) не резолвит POSIX-стиля
      `$TMP/bin` в `PATH`, реальный `gh.exe` перехватывает вызов раньше
      заглушки; воспроизведено НА НЕИЗМЕНЁННОМ `main` тем же прогоном —
      предсуществующее ограничение среды, не регрессия этого PR. Смоук
      обязан быть прогнан в CI (Ubuntu) как обычно.

## Гейт 4 — hands.yml (#805, сделано этим PR)

- [x] `scripts/hands/dsh_task.sh` вызывает `dsh_require_provider_chain`/
      `dsh_run_with_provider_chain` вместо `dsh_require_provider_env`/
      `dsh_run_with_retry`. Профиль затравлен ПЕРВЫМ провайдером цепочки
      (`chain[0]`) ДО первого `dsh` этого прогона (`dsh plugin add`,
      hands-streamer) — тот же приём, что уже доказан у `worker.yml` (#797).
- [x] `hands.yml` передаёт `DSH_PROVIDER_CHAIN`+`NVIDIA_API_KEY`; статичные
      `DEEPSEEK_BASE_URL`/`DEEPSEEK_MODEL` убраны из `env:` шага (их теперь
      выставляет сам `dsh_task.sh` из цепочки).
- [x] `all_providers_exhausted` в `dsh_task.sh` обрабатывается тем же путём,
      что `quota_exhausted`/`rate_limit_retry_budget_exceeded` (release-full,
      честное сообщение в журнал задачи).
- [x] Особый риск рук (bootstrap-событие журнала называет модель ДО первой
      попытки цепочки) — решён, не задокументирован как неустранимый:
      `bootstrap` теперь называет `chain[0]` ЯВНО как первого кандидата
      (`provider_chain.head_model`/`provider_chain.candidates`), не как факт
      «эта модель ответила»; факт, какой провайдер реально обслужил вызов,
      уходит уже существующим безусловным событием `agent_answer` новыми
      полями `provider`/`model`/`provider_chain_tried` (значение `$DSH_MODEL`/
      `$DSH_CHAIN_PROVIDER` после возврата `dsh_run_with_provider_chain` —
      эта функция перепатчивает профиль на КАЖДОЙ попытке, поэтому оба
      значения отражают именно последнего опробованного/успешного
      провайдера). Рассинхрон закрыт механизмом, не только прозой.
- [x] `scripts/lib/test/dsh-clients.smoke.sh` — сценарий `hands-rate-limit-budget`
      переведён на ожидание `all_providers_exhausted` (фикстура несёт РОВНО
      одного провайдера — тот же паттерн, что уже доказан для ai-review-сценариев
      этого файла); happy-path сценарий `hands` не тронут по ассертам
      (провайдер добавляет поля в `agent_answer`, не убирает существующие).
      Живой прогон смоука в среде разработки этого PR (Windows, git-bash)
      упирается в ТУ ЖЕ независимую от диффа проблему, что уже
      задокументирована для #797: `claim_task.py` (нативный Windows-
      интерпретатор Python) резолвит системный `gh.exe`, не POSIX-стиля
      `$TMP/bin` заглушку — воспроизведено на НЕИЗМЕНЁННОМ `main` тем же
      прогоном. Смоук обязан быть прогнан в CI (Ubuntu) как обычно.
- [x] `docs/agents/INFRA-GH.md` — строка инвентаря `hands.yml` обновлена
      (секреты/vars, `#727/#805`), `scripts/lib/test_infra_gh_inventory.py`
      зелёный.

Не пункт этого change (отдельная задача, не блокирует его завершение): белое
пятно #798 — первый элемент живого `vars.DSH_PROVIDER_CHAIN` (`NVIDIA-nano`)
не подтверждён реестром `confirmed-provider-models.json` (#737) — цепочка
молча пропускает его на каждом прогоне, включая `ai-review.yml`, `worker.yml`
и теперь `hands.yml`.
