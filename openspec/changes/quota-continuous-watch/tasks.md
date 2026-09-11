# Tasks: quota-continuous-watch (#605)

Работа реализована в PR #607 (ветка `agent/605-quota-continuous-watch`);
пункты отмечены по факту состояния кода на момент составления — каждый
несёт свой критерий приёмки.

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
- [x] Слепая зона одной страницы скана — исходы `SCAN_*` с нижней
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
      простоя, не страницей).
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

## Приёмка (пост-мерж, до этого change не завершён)

- [ ] Пост-мерж проверка видимого результата (не «CI зелёный»): первый
      прод-прогон `quota-watch.yml` на `main` выполнил шаг замера — в
      логе прогона строка «DO rows_read/сутки: N / 5 000 000 (…)» от
      `cheap_check`, а `scripts/measure/do_rows_read.py` трейс подтверждает
      данные Cloudflare; след дедупа (маркер состояния ресурса) появился
      в #120 после первого тика с замером. До этого пункта изменение
      считается незавершённым независимо от слияния PR.
