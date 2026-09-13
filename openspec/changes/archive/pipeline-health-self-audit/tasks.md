# Tasks: pipeline-health-self-audit

Разбивка реализации design.md. Каждый пункт — рабочий носитель (код + тест),
не заглушка. Пороги (окна, потолки, проценты) — константы `scripts/orchestra/
health_regression.py`/`scripts/orchestra/health_audit.py`, честно помечены в
design.md «Не подтверждено» как не выведенные из данных (истории снимков ещё
нет на момент реализации).

## Сборщик метрик + хранилище (design.md §1, §2.1, §2.2)

- [x] `scripts/measure/pipeline_health.py`: чистые функции метрик (PR age
  p50/p95, backlog по состояниям, worker success-rate, каденс пульса, merge
  throughput из Search API) над уже прод-формой ответов GitHub API.
- [x] Сборка снимка (`build_snapshot`) + I/O-обвязка (`collect`), переиспользует
  `pulse_guard.recent_runs`/`orchestra_tick_runs`, `review_labels.list_pages`,
  `quotas.collect_cloudflare` (DO cost) — второй копии транспорта не заводит.
  DO cost и GH rate remaining — честное «нет данных» (None), если токены не
  заданы или интроспекция не удалась (не фатально для остальных метрик).
  DO cost остаётся дневным приближением по факту вызова — прямого замера
  стоимости самого снимка (см. proposal.md, «Не подтверждено», п.2) здесь нет.
- [x] Хранилище — JSONL на ветке `data/pipeline-health` (git-транспорт по
  прецеденту `dispatch_tail.py`/`data/dispatch-latency-tail`), гейт «не чаще
  раза в календарные сутки UTC» (`should_snapshot`).
- [x] Тесты прод-формой (фикстуры `scripts/measure/fixtures/`) + мутация
  границ (percentile, гейт даты, backlog-классификация).

## Детектор регрессии (design.md §2.3)

- [x] `scripts/orchestra/health_regression.py`: baseline (медиана скользящего
  окна), streak ≥ MIN_REGRESSION_STREAK_DAYS, три исхода
  (`insufficient_data`/`ok`/`regression`/`fire`), 6 метрик из §1 (без
  дублирования backlog/pr_age в обе стороны — по одной регрессионной метрике
  на измерение, см. докстринг модуля).
- [x] Тесты: мутация boundary (порог `>=` не `>`, streak `>=` не `>`),
  честное `insufficient_data` при малой выборке, регрессия красится на
  смоделированной деградации и молчит на здоровом ряду.

## Само-аудит (design.md §3)

- [x] `scripts/orchestra/health_audit.py`: дедуп по отпечатку
  `regression:<метрика>` (поиск в открытых issues с меткой `self-audit`),
  свой суточный потолок `SELF_AUDIT_DAILY_CAP` (= число метрик), различение
  «обычный приоритет»/«пожар» одной функцией с двумя порогами, эскалация
  пожара тем же каналом, что `pulse_guard.escalate`.
- [x] Тело задачи — отпечаток, числа (baseline/сегодня/отклонение/streak),
  ссылка на снимок, честный потолок (копия принципа stall_detector, не новая
  формулировка).
- [x] Тесты: заводит задачу на регрессии, не заводит дубль при уже открытой
  задаче того же отпечатка, дедуп-комментарий по дню, потолок не превышается.
- [x] Метка `self-audit` — строка в `docs/agents/LABELS.md`
  (`test_label_registry.py` зелёный).

## Проводка (design.md §2.2, «Тормоз без газа»)

- [x] Шаг в `.github/workflows/orchestra.yml` (по образцу
  `stale_blocked_guard`/`waiting_owner_guard`: `continue-on-error: true`,
  свой блок env) — снимок (гейт раз/сутки) + регрессия + само-аудит одним
  вызовом `python scripts/orchestra/health_audit.py run`.
- [x] `openspec/changes/pipeline-health-self-audit/specs/pipeline-health/spec.md`
  — дельта-спека (ADDED requirements).

## Вне рамок этого прохода (названо, не реализовано)

- Точная подстройка порогов (BASELINE_WINDOW_DAYS и т.д.) по факту первых
  недель эксплуатации — данных ещё нет, будет отдельной узкой задачей после
  накопления истории (proposal.md, «Не подтверждено», п.1).
- Дашборд в морде dsh-edge (#407/#377) — потребитель снимка, не эта задача
  (design.md §5, proposal.md Scope «Out»).
- Пересечение с #224 (граф блокировок/приоритет) по общему модулю чтения
  issues/PR API — не выносится в этом проходе (design.md «Не подтверждено», п.5).
