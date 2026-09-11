Задача: #838.

# Tasks: anthropic-oauth-pool-standalone

- [x] 1. `scripts/lib/dsh-ci.sh`: `ANTHROPIC_OAUTH_POOL_RELEASE` (пин тега
      релиза, литерал по образцу `DSH_VERSION`), `dsh_install_anthropic_pool`
      (скачивание+sha256+распаковка ассета `PLUGINS_SUITE_OAUTH_ASSET`,
      независимо от `vars.PLUGINS_SUITE_URL`; гейт — наличие хотя бы одного
      из `ANTHROPIC_OAUTH_1`/`ANTHROPIC_OAUTH_2`, иначе notice+return 0),
      `dsh_mount_anthropic_pool` (`dsh plugin add` + структурная проверка
      `dsh --dump-config` на `^- id: anthropic-oauth-pool$`, fail loud).
- [x] 2. `dsh_import_anthropic_accounts` — для каждого заданного секрета
      пишет значение во временный файл mode 0600, зовёт
      `node <extracted>/bin/dsh-anthropic-pool.js add anthropic-N <файл>`,
      удаляет временный файл; fail loud при сбое импорта. **Плюс `unset`
      секрета из окружения сразу после импорта** — находка design.md
      («Секреты в доверенной границе ai-review»): `ANTHROPIC_OAUTH_1/2` не
      match-ат паттерн `*_KEY`/`*_TOKEN`/`*_SECRET`, которым DSH иначе сам
      прячет секреты от shell-тула модели в `ai-review` (trust-zone #18) —
      без явного `unset` живой OAuth JSON виден агенту, ревьюющему
      недоверенный PR, через `env`/`printenv`.
- [x] 3. `_dsh_patch_profile_anthropic_pool` + `dsh_run_with_pool_then_chain`
      (пул первым через `dsh_run_with_retry`, при отказе — существующая
      `dsh_run_with_provider_chain` без изменений; пул неактивен → сразу
      цепочка). `DSH_CHAIN_PROVIDER`/`DSH_CHAIN_TRIED`/`DSH_RUN_FAILURE_REASON`
      остаются честными в обоих путях.
- [x] 4. Подключить в `scripts/worker/task.sh`, `scripts/hands/dsh_task.sh`,
      `scripts/review/ai_dsh.sh`: вызов `dsh_install_anthropic_pool` +
      `dsh_import_anthropic_accounts` + `dsh_mount_anthropic_pool headless`
      в ту же точку, где уже стоят `dsh_install_plugins_suite`/
      `dsh_mount_plugins_suite`; замена финального
      `dsh_run_with_provider_chain` на `dsh_run_with_pool_then_chain`.
- [x] 5. `.github/workflows/worker.yml`, `hands.yml`, `ai-review.yml`:
      `ANTHROPIC_OAUTH_1: ${{ secrets.ANTHROPIC_OAUTH_1 }}`,
      `ANTHROPIC_OAUTH_2: ${{ secrets.ANTHROPIC_OAUTH_2 }}` в env шага DSH.
- [x] 6. Тест-гвардия `scripts/lib/test/dsh-anthropic-pool.guard.sh`,
      доказана мутацией:
      (а) нет секретов → `dsh_install_anthropic_pool` не качает, не падает,
          `DSH_ANTHROPIC_POOL_ACTIVE=0`;
      (б) секрет задан → `dsh_import_anthropic_accounts` кладёт
          `~/.dsh/anthropic-accounts/<id>.json` с `claudeAiOauth.
          {accessToken,refreshToken}` из фейкового JSON, И секретная
          переменная окружения отсутствует (`unset`) после возврата функции
          (доказательство находки «Секреты в доверенной границе ai-review»);
      (в) `dsh_run_with_pool_then_chain`: пул отвечает успехом → цепочка не
          запускается, `DSH_CHAIN_PROVIDER=anthropic-oauth-pool`; пул
          отказывает → откат на цепочку с честными
          `DSH_CHAIN_PROVIDER`/`DSH_CHAIN_TRIED` (включая
          `anthropic-oauth-pool` в `DSH_CHAIN_TRIED`); пул неактивен →
          поведение цепочки идентично состоянию ДО этого change.
      Регистрация шагом в `repo-ci.yml`, тем же местом, что соседние
      гвардии (`dsh-suite-chain-conflict.guard.sh` и т.п.).
- [x] 7. Дельта-спека `specs/journal-tasks-hands/spec.md` (ADDED-требование).

## Доработка (#859, живой инцидент PR #858, 2026-09-10)

- [x] 8. `dsh_import_anthropic_accounts` изолирует каждый секрет независимо
      (BOM-strip + jq-валидация JSON/полей ДО вызова `node ... add`) —
      битый секрет пропускается с `::warning::`, не роняет шаг; ни одного
      валидного аккаунта → пул тихо отключает себя
      (`DSH_ANTHROPIC_POOL_ACTIVE=0`), не падает. См. design.md, «Изоляция
      битого секрета».
- [x] 9. Тест-гвардия расширена секциями 6-8 (BOM восстанавливается и
      импортируется; сосед с битым JSON пропущен; все секреты биты →
      сквозной путь до цепочки), доказано мутацией (откат фикса
      воспроизводит исходный `SyntaxError` из живого инцидента).

## Доработка (#860, решение владельца 2026-09-10): сплит потребителей квоты

- [x] 10. Убрать пул из `ai-review.yml`/`scripts/review/ai_dsh.sh`: секреты
      `ANTHROPIC_OAUTH_1`/`ANTHROPIC_OAUTH_2` больше не в env шага DSH,
      `ai_dsh.sh` не вызывает `dsh_install_anthropic_pool`/
      `dsh_import_anthropic_accounts`/`dsh_mount_anthropic_pool` — прогон
      идёт напрямую `dsh_run_with_provider_chain` (дневная цепочка GLM/ZAI,
      `config/provider-usage.json`, #857). `worker.yml`/`hands.yml` не
      тронуты — пул там остаётся первым перед цепочкой, как в п.3-5 выше.
      Причина: недельная квота Claude (разработка) и дневная квота GLM/ZAI
      (ревью) не должны жечься одним потребителем.
- [x] 11. Дельта-спека `specs/journal-tasks-hands/spec.md` — MODIFIED-секции
      «Проводка секретов» (было: три канала, стало: только worker/hands) и
      уточнена область действия «Выбор провайдера — пул первым».

## Закрывающая проверка (после этого PR, требует секретов владельца)

- [ ] Владелец кладёт `ANTHROPIC_OAUTH_1`/`ANTHROPIC_OAUTH_2`
      (`gh secret set`, формат — README PR/issue #838). Живой прогон
      `worker.yml`/`hands.yml`: в логе видно монтаж `anthropic-oauth-pool`,
      импорт аккаунтов, попытку пула ПЕРВОЙ; при успехе —
      `DSH_CHAIN_PROVIDER=anthropic-oauth-pool` в отчёте прогона.
      `ai-review.yml` в эту проверку НЕ входит (#860) — там пула нет
      структурно. Без секретов эта проверка невозможна — не входит в
      критерий готовности самого PR (Scope/Out), фиксируется отдельным
      комментарием в #838 после того, как секреты появятся.
