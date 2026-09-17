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

## 4. Находки ai-review PR #1276 (второй круг)

- [x] Блокер 1 (эскалация-до-итога-прогона, Исход 3): решение «второе
      подряд» откладывается, пока существует прогон worker.yml, начавшийся
      после маркера повтора и не завершённый
      (`scheduler.unattributed_retry_in_flight`);
      поведенческий тест с живым `in_progress`-прогоном после маркера
      (`test_dispatch_ai_review_rework_defers_second_unattributed_while_retry_in_flight`)
      + газ дефера
      (`test_dispatch_ai_review_rework_escalates_second_unattributed_after_retry_finished`)
- [x] Блокер 2 (цена расширенного окна): след страницы матчится об ОДИН
      фетч комментариев задачи на страницу
      (`run_claimed_in_comments`; `run_claimed_task` — для одиночных
      потребителей); поведенческие тесты цены
      (`test_last_worker_run_since_reads_task_comments_once_per_page`,
      `test_last_worker_run_without_since_reads_task_comments_once`)
- [x] Чеклист: подлинное возражение с цитатой дисПатча не выбрасывается
      фильтром (`rebuttal_is_genuine`: маркер возражения не позже маркера
      дисптча; юнит-тест + цитата в фикстуре
      `..._surfaces_rebuttal`)
- [x] Чеклист: непарный RuntimeError в ветке возражения — чтение падает →
      решение откладывается с наблюдением, не «без объяснения»
- [x] Чеклист: дубль чтения маркера повтора — проба
      `pending_unattributed_retry` переиспользуется веткой Исхода 3
      (стоимость закреплена ассертом в тесте дефера)
- [x] Мутации второго круга исполнены (см. `proposal.md`, «Проверено»,
      мутации 5–8) — сняты по очереди, зафиксирован красный прогон,
      возвращены
