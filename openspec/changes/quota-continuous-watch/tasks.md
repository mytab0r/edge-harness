# Tasks: quota-continuous-watch (#605, #1100)

Работа реализована в PR #607 (ветка `agent/605-quota-continuous-watch`);
пункты отмечены по факту состояния кода на момент составления — каждый
несёт свой критерий приёмки. Раздел «Тренд и третье состояние» добавлен
PR #1112 (#1100) — живой инцидент 2026-09-13 показал, что дедуп по
переходу структурно не может предупредить о РОСТЕ, и что дедуп состояния
был структурно мёртв (см. дельта-спеку, раздел MODIFIED).

## Каденция и стоимость

- [x] `quota-watch.yml` — триггер `pull_request` (основной носитель) +
      cron `3,18,33,48 * * * *` (страховка тихих часов) +
      `workflow_dispatch`. Критерий: интервал обоснован числами профиля
      дня инцидента в докстринге `quota_watch.py`; доставка cron ~6.7%
      против событий 32/32 процитирована из
      `docs/research/21-github-actions.md`.
- [x] Concurrency-группа `quota-watch` (`cancel-in-progress: false`).
      Критерий: два перекрывшихся `pull_request`-прогона сериализованы —
      гейт каждого видит завершённый предыдущий; гонка двойного Telegram
      на одном переходе закрыта сериализацией.
- [x] Троттлинг по реальным замерам: гейт-шаг (`gate_main`) + шаг замера
      c `if: steps.gate.outputs.proceed`; «реальный» = `conclusion`
      `success`/`failure` шага замера, `skipped` окно не открывает.
      Критерий: `test_workflow_step_names_match_constants` (синхронность
      имён шагов YAML ↔ констант), тесты
      `test_scan_measurement_history_*` (мутационные: skipped-прогоны
      не троттлят, полный-без-замера лист → `SCAN_CEILING`, а не холодный
      старт; failure-прогон троттлит, но не доказывает успех).

## Алерт, дедуп, автозадача

- [x] Один канал решения `quota_alert.check_and_alert` для дешёвой
      проверки и полного среза, дедуп по переходу `ok`↔`breach` (маркер
      состояния ресурса в #120). Критерий:
      `test_quota_alert.py::test_recovery_transition_alerts_without_creating_task`
      и соседние — переход алертит, повтор — нет.
- [x] Автозадача на breach через `scripts/gh/issue-create` с
      `task`/`area:process`/`auto-detected`/`quota-breach`; несостоявшееся
      заведение не пишет маркер (повтор попытки следующим прогоном).
      Критерий: тесты `create_or_note_task`/`check_and_alert` на отказ
      issue-create; метка `quota-breach` существует в репозитории.
- [x] Кандидаты гвардии дублей различаются по ресурсу: улика уходит
      только в задачу про ТОТ ЖЕ ресурс (метка ресурса в заголовке),
      похожие задачи других ресурсов заводят свою задачу с
      `--confirm-not-duplicate`. Критерий:
      `test_create_task_other_resource_candidates_confirmed_not_duplicate`
      (мутационный), `test_create_task_mixed_candidates_prefers_same_
      resource`; метка `quota-breach` существует в репозитории
      (проверено `gh label list` 2026-09-11, цвет B60205; создана в раннем
      раунде этого PR — до первого реального breach, а не на нём).
- [x] Полный срез покрывает все эскалируемые строки `quotas.py`, включая
      GitHub REST/GraphQL rate limit; пропуски — громкие.
      Критерий: `test_full_sweep_escalates_github_rest_and_graphql_rate_limit`
      (мутационный), `test_full_sweep_skips_rows_without_key_or_pct`.

## Самонаблюдение сторожа

- [x] Простой УСПЕШНОГО замера — громкий сигнал с эпизодным дедупом
      (`STALE_MARKER`/`STALE_RESOLVED_MARKER`), закрытием эпизода ТОЛЬКО
      точным свежим успехом и классифицированной причиной (не гипотезы);
      троттлинг и простой — два возраста одного скана (`MeasurementScan`):
      устойчивый отказ замера (протухший CF-токен) даёт сигнал через
      `MEASUREMENT_STALE_MINUTES`, а не гасится свежими failure-прогонами
      (found: ревью PR #607, head eae35fc). Критерий: тесты
      `test_stale_alert_*`, `test_classify_measurement_absence_*`,
      `test_gate_main_escalates_true_transition_*`,
      `test_scan_measurement_history_sustained_failure_*`,
      `test_gate_main_escalates_during_sustained_measurement_failure`
      (мутационно: кормление канала простоя attempt_age краснит их).
- [x] Слепая зона одной страницы скана — пагинация до порога простоя
      с жёстким потолком `MEASUREMENT_SCAN_MAX_PAGES` (found: ревью PR
      #607, head 345a64f — в шторме PR-событий ~1 прогон/мин страница 30
      покрывает ~15–30 минут и скользит, порог 45 мин нижней границей не
      достигался никогда: устойчивый отказ замера оставался тихим) —
      исходы `SCAN_*` с нижней
      границей простоя и честной формулировкой; нижняя граница ниже
      порога не закрывает эпизод; неосмотренные шаги прогонов = 
      «история недоступна». Критерий:
      `test_scan_measurement_history_ceiling_when_page_full_below_stale_
      threshold` (мутационно доказан: возврат `None, True` краснит
      тесты), `test_gate_main_escalates_ceiling_bound_with_honest_
      wording`, `test_gate_main_ceiling_bound_below_stale_threshold_...`,
      `test_scan_measurement_history_stale_proven_stops_before_inspecting_
      boundary_run`, `test_scan_measurement_history_early_stop_bounds_
      jobs_calls_by_stale_window` (стоимость тика ограничена окном
      простоя, не страницей), пагинация: `test_scan_measurement_history_
      paginates_and_finds_success_on_second_page`,
      `test_scan_measurement_history_stale_proven_on_second_page_without_
      inspecting_boundary_run`,
      `test_scan_measurement_history_ceiling_only_after_page_cap`
      (мутационно: потолок страниц = 1 краснит тесты пагинации — блокер
      «шторм держит страницу скользящей» воспроизводится).
- [x] Неслитый код не будит владельца: сверка исполняемой копии workflow
      с `main` в трёх состояниях (matches/differs/unknown), `unknown`
      не глушит сигнал. Критерий: `test_workflow_version_check_*`,
      `test_gate_main_escalates_with_honest_note_when_version_check_fails`.
- [x] Несостоявшийся замер краснит прогон. Критерий:
      `test_measure_main_exits_nonzero_without_credentials`,
      `test_measure_main_exits_nonzero_when_measurement_itself_fails`.
- [x] Вердикт доставки не выбрасывается ни одним каналом (found: ревью PR
      #607, heads 5824e75/345a64f): гейт применяет оба предиката
      (`escalation_channel_failed`/`escalation_dedup_carrier_failed`) к
      `stale_alert`; `measure_main` — к результатам `cheap_check` и
      `full_sweep` (`_delivery_exit_failed`, тексты ошибок различают формы
      отказа); тихая запись маркера (первое наблюдение в норме) возвращает
      вердикт `pulse_guard.carrier_write_verdict` и ловится предикатом
      носителя дедупа, source-гвардия литералов расширена на
      `quota_alert.py`. Критерий:
      `test_gate_main_exits_nonzero_*` (гейт),
      `test_measure_main_exits_nonzero_when_dedup_carrier_fails`,
      `test_measure_main_exits_nonzero_when_full_sweep_dedup_carrier_fails`
      (мутационно: возврат к `_channel_failed` краснит),
      `test_first_observation_marker_write_failure_is_predicate_visible`
      (мутационно: немаркированная строка отказа краснит).
- [x] Чтение маркеров #120 ограничено свежей страницей
      (`MARKER_SCAN_PAGES`, одно место правды в `quota_alert.py`): и
      stale-эпизоды, и дедуп состояния ресурса. Критерий:
      `test_stale_alert_reads_only_fresh_page_of_watchdog_history`,
      `test_close_stale_episode_reads_only_fresh_page`,
      `test_last_state_reads_only_fresh_page_of_watchdog_history` —
      запрос страницы 2 краснит тест.

## Документация

- [x] Шапка `quotas.yml` обновлена явно как принятое «отдельное решение»
      (файл не мутирован молча). Критерий: дифф PR, комментарий шапки.
- [x] `docs/agents/LABELS.md` — строки `auto-detected` (третий сеятель) и
      `quota-breach` описывают фактический механизм; гвардия
      `test_label_registry` сверяет реестр с кодом.
- [x] Этот change: proposal + дельта-спека + tasks. Критерий: каталог
      существует, пункты выше отражают состояние кода.

## Тренд и третье состояние (#1100, PR #1112)

- [x] Корень дедупа: `pulse_guard.all_issue_comments(max_pages=N)` читал
      буквальную `page=1..N` как «свежие страницы» — у эндпоинта GitHub
      «List issue comments» нет `sort`/`direction`, страница 1 растущей
      истории #120 ВСЕГДА самая старая (проверено живьём: `page=1` при 952
      комментариях вернул комментарий от 2026-08-31). Фикс: метаданные
      issue (число комментариев, один вызов) → настоящая последняя
      страница. Критерий:
      `test_pulse_guard.py::test_all_issue_comments_without_max_pages_
      ignores_issue_metadata`,
      `test_quota_watch.py::test_all_issue_comments_max_pages_bounds_
      traversal`, `test_quota_alert.py::test_last_state_reads_only_fresh_
      page_of_watchdog_history` (мутационно: возврат к чтению с начала
      красит все три).
- [x] Третье состояние `STATE_APPROACHING` (`quota_alert.classify_state`)
      — по тренду (`last_reading`/`record_reading`), не только по текущему
      pct. Горизонт 45 мин (3×`CHECK_INTERVAL_MINUTES`, синхронность держит
      `test_quota_watch.py::test_trend_horizon_matches_check_interval`).
      Approaching эскалирует без автозадачи; переход approaching→ok несёт
      текст, отличный от breach→ok (не заявляет ложного пересечения
      порога). Критерий: `test_quota_alert.py::test_approaching_*`,
      `test_classify_state_*`, `test_recovery_from_approaching_does_not_
      claim_threshold_was_crossed`, `test_recovery_from_breach_still_
      mentions_the_task`.
- [x] Числовой носитель тренда — редактируется на месте
      (`pulse_guard.edit_issue_comment`), не растёт с частотой тиков.
      Честная деградация («съезжает за страницу по мере роста #120,
      теряет 1 сэмпл, самоисцеляется следующим тиком») названа в
      докстринге `record_reading` и закреплена тестом
      `test_reading_carrier_falling_off_fresh_page_self_heals_next_tick`.
- [x] Собственная каденция для GitHub REST/GraphQL rate limit — новый шаг
      workflow `github-rate-limit`, без CF-гейта (чтение `rate_limit`
      бесплатно, проверено живьём), с независимым троттлингом
      (`RATE_LIMIT_MIN_INTERVAL_MINUTES`). Стоимость измерена, не угадана
      (см. докстринг константы в `quota_watch.py`): 4 REST-вызова на
      троттлинг-тик, 10 на замер-тик без перехода (было 14 до передачи
      уже прочитанного показания в `check_and_alert` через `prev_reading=`
      — found: ревью PR #1112), 12 на первое наблюдение (было 16).
      Критерий: `test_quota_watch.py::test_github_rate_limit_main_*`.
- [x] Воспроизведение: `test_quota_alert.py::test_reproduction_1100_
      trend_fires_before_exhaustion` — на исторических точках issue #1100
      сигнал срабатывает в 07:33, за 53 минуты до исчерпания (08:26) и за
      46 минут до первого реального отказа оркестратора (08:19).

## Приёмка (пост-мерж, до этого change не завершён)

- [ ] Пост-мерж проверка видимого результата (не «CI зелёный»): первый
      прод-прогон `quota-watch.yml` на `main` выполнил шаг замера — в
      логе прогона строка «DO rows_read/сутки: N / 5 000 000 (…)» от
      `cheap_check`, а `scripts/measure/do_rows_read.py` трейс подтверждает
      данные Cloudflare; след дедупа (маркер состояния ресурса) появился
      в #120 после первого тика с замером. До этого пункта изменение
      считается незавершённым независимо от слияния PR.
- [ ] Пост-мерж проверка видимого результата тренда (#1100): первый
      прод-прогон `github-rate-limit` записал числовой носитель
      (`[quota: замер gh_rest_rate_limit_hour = N% at …]`) в #120, и
      второй прогон (15+ минут спустя) отредактировал ЕГО ЖЕ (не завёл
      новый) — счётчик комментариев #120 от этого шага не растёт.
