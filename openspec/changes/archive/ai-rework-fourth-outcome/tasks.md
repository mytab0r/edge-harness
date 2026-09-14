# Задачи: ai-rework-fourth-outcome (#1274)

## 1. Код

- [x] `pulse_guard.recent_runs` принимает `page` (без него поведение не
      меняется)
- [x] `pulse_guard.LAST_RUN_LOOKUP_PAGES` — потолок страниц расширенного
      поиска
- [x] `pulse_guard.AI_REWORK_SECOND_CHANCE_MARKER`,
      `AI_REWORK_UNATTRIBUTED_RETRY_MARKER`, `AI_REWORK_REBUTTAL_MARKER`
- [x] `scheduler.last_worker_run` принимает `since` (расширенное окно
      постранично, старое поведение без анкера не меняется)
- [x] `dispatch_ai_review_rework`: Исход 2 (success) — бонус-заход с явным
      контрактом перед эскалацией, эскалация второго подряд совпадения
      цитирует возражение агента, если было
- [x] `dispatch_ai_review_rework`: Исход 3 (не атрибутирован) — бесплатный
      повтор перед эскалацией, эскалация второго подряд совпадения называет
      дефект атрибуции
- [x] Гейт входа в решающую ветку учитывает висящий маркер бонусного
      повтора Исхода 3 независимо от `attempts` (иначе бесконечный
      автодисПатч без эскалации — газ без тормоза)

## 2. Тесты (`scripts/orchestra/test_scheduler.py`)

- [x] `_ai_rework_base_fixture`/`_ai_rework_second_round_fixture` —
      прод-форма фикстур (маршрут `per_page=100&page=1`, реальные номера
      PR/задач/прогонов из живых инцидентов)
- [x] Исход 2, первое обнаружение — бонус-заход, не эскалация
      (`test_dispatch_ai_review_rework_grants_second_chance_before_escalating_on_success`)
- [x] Исход 2, второе обнаружение — эскалация называет факт
      (`test_dispatch_ai_review_rework_escalates_after_second_success_names_the_fact`)
- [x] Исход 2, возражение агента — эскалация цитирует его
      (`test_dispatch_ai_review_rework_second_success_escalation_surfaces_rebuttal`)
- [x] Расширенное окно атрибуции находит прогон вне топ-10 — прод-форма
      живого случая PR #804/задача #720
      (`test_dispatch_ai_review_rework_extended_window_finds_run_outside_top10`)
- [x] Исход 3, первое обнаружение — бесплатный повтор
      (`test_dispatch_ai_review_rework_grants_free_retry_before_escalating_unattributed`)
- [x] Исход 3, второе обнаружение — эскалация называет дефект атрибуции
      (`test_dispatch_ai_review_rework_escalates_second_unattributed_as_attribution_defect`)
- [x] `test_dispatch_ai_review_rework_escalates_while_worker_active`
      переведён на `conclusion='timed_out'` — не путает независимость
      решения от занятости воркера с двухшаговым контрактом Исхода 2
- [x] Мутации исполнены (см. `proposal.md`, «Проверено») — сняты по
      очереди, зафиксирован красный прогон, возвращены

## 3. Документация

- [x] `proposal.md` — зачем, что делается, что вне рамок, живой пример PR
      #804
- [x] `specs/journal-tasks-hands/spec.md` — MODIFIED-требования на все три
      изменённых поведения
- [x] PR называет исход, который получил бы PR #804 по новому коду
