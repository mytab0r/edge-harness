Задача: #857.

# Tasks: provider-quota-gating

Один поток (нет естественного frontend/backend разреза) — раздел один,
порядок задач важен (blocked by отмечен явно).

## Носитель + запись (пульс)

- [x] 1.1 `scripts/orchestra/provider_quota_state.py` — новый модуль:
      `QUOTA_VAR_NAME`, `parse_reset_hint_pairs`, `merge_reset_hints`,
      `expire_stale`, `load_quota_state`, `save_quota_state`.
- [x] 1.2 `scripts/orchestra/scheduler.py`: импорт модуля (importlib-паттерн,
      как `review_labels`/`free_task`), `parse_reset_hint_dates` делегирует
      в `provider_quota_state.parse_reset_hint_pairs` (одно место правды на
      формат дат). Новая функция `sync_provider_quota_state`, вызов из
      `trigger_ai_review` в ветке `needs_retry` там, где уже вычисляется
      `facts.get("reset-at", "")`.
- [x] 1.3 Юнит-тесты `scripts/orchestra/test_provider_quota_state.py` —
      чистые функции без сети + `load_quota_state`/`save_quota_state` с
      фейковым `gh_func`.
- [x] 1.4 Тесты в `scripts/orchestra/test_scheduler.py`: `_DEFAULT_ROUTES`
      несёт 404-заглушку GET и success-заглушку PATCH для
      `actions/variables/DSH_PROVIDER_QUOTA_UNTIL` (не ломает существующие
      тесты); новые тесты — запись state из свежего `reset-at`, снятие
      протухшей записи, отсутствие сетевого вызова при пустом `reset-at`.

## Чтение (чейн-раннер) — blocked by: 1 (нужен `QUOTA_VAR_NAME`, чтобы имя совпадало)

- [x] 2.1 `scripts/lib/dsh-ci.sh`: `dsh_provider_quota_gate_skip`, вызов в
      цикле `dsh_run_with_provider_chain` ПЕРЕД проверкой секрета/
      подтверждённой модели. Валидация JSON один раз в начале функции
      (fail-open на невалидном значении, `::warning::`).
- [x] 2.2 Новый сценарий(и) в
      `scripts/lib/test/dsh-provider-chain.smoke.sh`: пропуск провайдера
      с будущим `reset_iso` (доказано мутацией — снял вызов гейта,
      сценарий покраснел), провайдер с прошедшим `reset_iso` пробуется как
      обычно.
- [x] 2.3 Три workflow (`ai-review.yml`, `worker.yml`, `hands.yml`):
      `DSH_PROVIDER_QUOTA_UNTIL: ${{ vars.DSH_PROVIDER_QUOTA_UNTIL }}` в
      `env:` того же шага, что уже несёт `DSH_PROVIDER_CHAIN`. Никаких
      новых прав токенов — переменная читается контекстом YAML, не `gh
      api`.

## Гвардия границы доверия

- [x] 3.1 `scripts/lib/test_provider_quota_state_guard.py` — ни один
      `.github/workflows/*.yml` не содержит `gh variable set
      DSH_PROVIDER_QUOTA_UNTIL`; `save_quota_state(` не вызывается за
      пределами `scripts/orchestra/**`.

## Цепочка провайдеров: GLM/ZAI первыми — blocked by: 2 (небезопасно без гейта)

- [x] 4.1 `config/provider-usage.json`: `GLM` первым, `ZAI`(`glm-5`,
      `ZAI_1_API_KEY`) вторым (новая запись, не подтверждена реестром
      #737 — см. design.md «Не подтверждено»). Остаток цепочки (#848) не
      трогается.
- [x] 4.2 `docs/agents/LLM-PROVIDER-USAGE.md` — таблица пересчитана
      `scripts/lib/collect_provider_usage.py` (9 провайдеров), добавлен
      абзац про новый порядок/механизм.
- [x] 4.3 `docs/runbooks/switch-llm-provider.md` — снята стухшая заметка
      «NVIDIA первым / GLM исчерпан до 2026-09-10», добавлено разъяснение
      про приоритет манифеста над `vars.DSH_PROVIDER_CHAIN` и про то, что
      персистентный гейт снимает нужду вручную переставлять порядок под
      квоту.
- [ ] 4.4 (владелец, вне PR) `gh variable set DSH_PROVIDER_CHAIN --body
      '<JSON из финального ответа апply-агента>'` — держит `vars.*` в
      синхроне с манифестом на случай, если манифест когда-нибудь исчезнет
      (переходный фоллбэк, design.md llm-provider-usage-manifest). Само
      поведение каналов НЕ изменится (манифест уже приоритетнее) — это
      только гигиена документации/фоллбэка.

## Проверка

- [x] 5.1 `python -m pytest scripts/orchestra/test_provider_quota_state.py
      scripts/orchestra/test_scheduler.py scripts/lib/test_provider_quota_state_guard.py
      scripts/lib/test_provider_usage_registry.py -q`
- [x] 5.2 `bash scripts/lib/test/dsh-provider-chain.smoke.sh`
- [x] 5.3 `python scripts/orchestra/repo_invariants.py` (инвариант 11 —
      манифест валиден для всех трёх consumer'ов)
