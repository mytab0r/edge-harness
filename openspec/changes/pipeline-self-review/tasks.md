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

## Доработка по запросу владельца (2026-09-12): регрессия #878

- [x] `fetch_workflow_runs_window` — адаптивная пагинация прогонов
      workflow (покрывает окно ЦЕЛИКОМ, не одну страницу), потолок
      `WORKFLOW_RUNS_MAX_PAGES=8` как броня, не рабочий режим.
- [x] `compute_before_after`/`fetch_recent_merges`/`gather_merge_correlation`
      — сигнал «доля успеха workflow до/после слияния», `BEFORE_AFTER_HOURS=8`
      подобран живым замером против реального инцидента #878 (design.md §5.4).
- [x] `gather_label_churn` — исключение `WATCHDOG_ISSUE` из кандидатов
      (найдено при замере цены: 10 страниц ради фильтра, отбрасывающего
      почти всё) — цена прогона 162 → 111 запросов.
- [x] `OPEN_QUESTION` — оговорка «корреляция ≠ причинность» текстом внутри
      промпта, не только в документации.

## Тесты и гвардии

- [x] `scripts/orchestra/test_self_review.py` — 48 тестов (контракт
      находок, жёсткий фильтр факта, отпечаток, дедуп+потолок,
      наблюдаемость, адаптивная пагинация workflow, корреляция
      слияние→до/после на РЕАЛЬНЫХ данных инцидента #878, ограниченность
      churn меток (число запросов + исключение WATCHDOG_ISSUE), fail-loud
      на отказ транспорта).
- [x] `scripts/ci/guards/self-review-guard.sh` — регистрация в каталоге
      гвардий (#749), без правки `repo-ci.yml`.
- [x] Доказательство мутацией `has_verifiable_fact`/`fingerprint`/
      `compute_before_after::success_rate` — снятие тела красит тесты,
      возврат — зелёные (design.md §7).

## Документация

- [x] `docs/agents/LABELS.md` — реестр меток `self-review`/
      `self-review:knowledge`/`self-review:instrument` (тормоз/газ).
- [x] `docs/agents/INFRA-GH.md` — строка инвентаря `self-review.yml`.
- [x] `scripts/lib/test_dispatch_token_usage.py::EXPECTED_WORKFLOWS` —
      сознательная правка списка (новый workflow).
- [x] `docs/agents/LLM-PROVIDER-USAGE.md` — честный пробел про
      `self-review` вне `CONSUMERS` (см. выше).

## Доказательство работоспособности

- [x] Живой прогон #1 `self_review.py gather` (окно 120ч) — нашёл (б)/(в),
      честно НЕ нашёл (а), причина названа — design.md §5.0.
- [x] Живой прогон #2, после доработки (окно 72ч, продакшн) — все три
      эталона в одном дайджесте: (а) через `merge_correlation`
      (delta=-0.75), (б) `label_churn` #782 (59 переключений), (в)
      `deploy-dsh-edge.yml` 6/10 — design.md §5.1-§5.5.
- [x] Цена прогона измерена числом дважды (162 → 111 запросов, после
      исключения WATCHDOG_ISSUE) — design.md §1, §3.2.
- [x] PR: https://github.com/mytab0r/edge-harness/pull/1026

## Вне рамок этой задачи (честно названо, не молчаливый пропуск)

- Реальный прогон `investigate`-шага (вызов модели через `ai_dsh.sh`)
  целиком не воспроизведён — цена одного лишнего дорогого вызова цепочки
  провайдеров признана неоправданной в рамках разработки; сам скрипт
  проверен транспортно тем же путём, что уже работает в `ai-review.yml`.
- `BEFORE_AFTER_HOURS=8` — параметр, подобранный против ОДНОГО известного
  инцидента (design.md §5.4, §8 п.2), не теоретически выведенный — граница
  названа явно, не скрыта.
- Регистрация `self-review` в `scripts/lib/collect_provider_usage.py::
  CONSUMERS`/таблице `docs/agents/LLM-PROVIDER-USAGE.md` — отложена до
  освобождения `repo_invariants.py` от параллельных PR (#944/#831/#1020/#811).
