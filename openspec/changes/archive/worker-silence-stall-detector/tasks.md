# Tasks: worker-silence-stall-detector (#1085)

- [x] `scheduler.py::_session_progress_tip` — максимальный `seq` хвоста
      сессии `harness-<N>` через `session.history {maxMessages:1}`, честная
      деградация при недоступности признака.
- [x] `scheduler.py::_morde_rpc` — параметр `mutating` (умолчание `True`,
      сохраняет прежнее поведение для `workspace.archiveSession`); чтение
      (`session.history`) вызывается с `mutating=False` — не гейтится
      write-guard'ом вне CI (некритичное замечание ai-review PR #1089).
- [x] `scheduler.py::_worker_silence_reason` — маркер прогресса ОДНИМ
      редактируемым на месте комментарием в WATCHDOG_ISSUE (`edit_issue_
      comment`/PATCH, приём #1100), сравнение `seq` с последним известным.
- [x] `scheduler.py::_stalled_run_task_number` — резолвится КАЖДЫЙ пульс
      (не с возраста ≥150) — иначе тишина не может сработать раньше
      удвоенного порога (найдено ai-review PR #1089, блокирующая находка).
- [x] `scheduler.py::reap_stalled_worker_run` — приоритет: тишина, когда
      доступна, решает одна (растёт — жив без возрастного потолка вовсе);
      возраст — фоллбэк только на недоступность признака (найдено ai-review
      PR #1089, вторая блокирующая находка — растущая сессия не спасала
      прогон старше возрастного потолка в первой версии).
- [x] Тесты: `_session_progress_tip` (контракт `session.history`,
      деградации), `_worker_silence_reason` (базовый маркер, рост seq,
      тишина ниже/выше порога, деградации), `_morde_rpc` (чтение не
      гейтится write-guard'ом), `reap_stalled_worker_run` (тишина ловит
      раньше возраста; растущая сессия переживает возрастной потолок;
      база пишется с раннего пульса, не с 150-й минуты).
- [x] `scripts/lib/test_pagination_guard.py::ALLOWED_SINGLE_PAGE_CALLS` —
      запись для `_active_worker_run` (вынесен из `stalled_worker_run`).
- [x] `scheduler.py::_worker_silence_reason`/`WORKER_SILENCE_BASELINE_
      GRACE_MINUTES` — «маркера нет» отличает генуинное первое наблюдение
      (прогон младше 30 мин) от подозрительного отсутствия (прогон старше,
      маркер выпал из окна `max_pages` под шторм других комментариев #120)
      — второе НЕ подтверждает жизнь, деградация к возрастному порогу
      (найдено ai-review PR #1089, третий раунд, блокирующая находка).
- [x] Дельта-спека `specs/journal-tasks-hands/spec.md`.
- [x] `python -m pytest scripts/orchestra/test_scheduler.py scripts/lib/test_pagination_guard.py -q` — зелёные.

## Вне рамок

- Бюджет суммарного времени цепочки провайдеров (`DSH_TIMEOUT_SECS` ×
  число записей цепочки) — отдельная задача #1160 (см. #1141), другой
  рычаг той же проблемы, не заменяет и не дублирует этот механизм.
- Подключение heartbeat (`HANDS_TOKEN`/`HARNESS_URL` в окружении
  `orchestra.yml`) как более точного признака — решение владельца (то же
  «вне рамок», что уже названо в `archive/worker-stall-detection`).
