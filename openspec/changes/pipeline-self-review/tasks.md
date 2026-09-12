# Tasks: периодическая саморевизия конвейера (#1025)

## Backend

- [x] `scripts/orchestra/self_review.py` — gather (сырые данные, дешёвый
      churn меток с потолком запросов, fail-loud на отказ транспорта),
      контракт находок (`parse_findings`, `has_verifiable_fact`),
      классификация по трём исходам (дефект/знание/инструмент), дедуп в два
      слоя (отпечаток + токенная похожесть), суточный потолок, apply (CLI).
- [x] `config/self-review-checklist.json` — чек-лист известных классов
      (данные, не код), семь пунктов из задания.
- [x] `.github/workflows/self-review.yml` — трёхшаговый трест-контур
      (gather/investigate/apply), cron раз в 6 часов + workflow_dispatch.
- [x] Провайдер: `config/provider-usage.json` несёт
      `usage["self-review"] = "default-chain"` — назначение читается
      напрямую bash-путём (`dsh_load_provider_chain_from_manifest`),
      регистрация в `scripts/lib/collect_provider_usage.py::CONSUMERS`
      отложена (файл делит инвариант с `repo_invariants.py`, который занят
      параллельными PR — см. честный пробел в
      `docs/agents/LLM-PROVIDER-USAGE.md`).

## Тесты и гвардии

- [x] `scripts/orchestra/test_self_review.py` — 35 тестов (контракт
      находок, жёсткий фильтр факта, отпечаток, дедуп+потолок,
      наблюдаемость, ограниченность churn меток, fail-loud на отказ
      транспорта).
- [x] `scripts/ci/guards/self-review-guard.sh` — регистрация в каталоге
      гвардий (#749), без правки `repo-ci.yml`.
- [x] Доказательство мутацией `has_verifiable_fact`/`fingerprint` —
      снятие тела красит тесты, возврат — зелёные (design.md §7).

## Документация

- [x] `docs/agents/LABELS.md` — реестр меток `self-review`/
      `self-review:knowledge`/`self-review:instrument` (тормоз/газ).
- [x] `docs/agents/INFRA-GH.md` — строка инвентаря `self-review.yml`.
- [x] `scripts/lib/test_dispatch_token_usage.py::EXPECTED_WORKFLOWS` —
      сознательная правка списка (новый workflow).
- [x] `docs/agents/LLM-PROVIDER-USAGE.md` — честный пробел про
      `self-review` вне `CONSUMERS` (см. выше).

## Доказательство работоспособности

- [x] Живой прогон `self_review.py gather` против `mytab0r/edge-harness`
      (окно 2026-09-07..2026-09-12) — дословный разбор, что находит
      механизм сам, без подсказки класса: design.md §5.

## Вне рамок этой задачи (честно названо, не молчаливый пропуск)

- Реальный прогон `investigate`-шага (вызов модели через `ai_dsh.sh`)
  целиком не воспроизведён — цена одного лишнего дорогого вызова цепочки
  провайдеров признана неоправданной в рамках разработки; сам скрипт
  проверен транспортно тем же путём, что уже работает в `ai-review.yml`.
- Ограничение потолка выборки (§5.3 design.md, срезает старый инцидент
  worker.yml) — названо явно, не исправлено: кандидат в отдельную задачу
  класса «инструмент», а не забытый пробел этой.
- Регистрация `self-review` в `scripts/lib/collect_provider_usage.py::
  CONSUMERS`/таблице `docs/agents/LLM-PROVIDER-USAGE.md` — отложена до
  освобождения `repo_invariants.py` от параллельных PR (#944/#831/#1020/#811).
