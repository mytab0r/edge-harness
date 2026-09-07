# ADR 0015. Носитель класса «тест зависит от настенных часов» — freezegun, не libfaketime

- **Дата:** 2026-09-07
- **Статус:** принято
- **Смежное:** issue #649, задача #643/PR #646 (живой случай, оплативший
  решение), [`scripts/conftest.py`](../../scripts/conftest.py),
  [`scripts/measure/clock_shift_suite.py`](../../scripts/measure/clock_shift_suite.py),
  [`.github/workflows/clock-shift-tests.yml`](../../.github/workflows/clock-shift-tests.yml)

## Контекст

`scripts/measure/test_dispatch_tail.py::test_dispatch_failure_writes_note_not_row`
истёк по фикстуре 2026-08-31 против `MAX_CAMPAIGN_DAYS=7` и стал перманентно
красным в обязательном job'е `test` (защита `main`) 2026-09-07 13:00 UTC, без
единого алерта — блокировал слияние 33 открытых PR (#643, фикс — PR #646).

Класс шире одного теста: фикстура несёт абсолютную дату, код сравнивает её с
порогом, который меряет от системного «сейчас» (`datetime.now()`/`time.time()`),
а не от переданного аргумента. Статический grep по `datetime.now(`/`utcnow(`/
`time.time()`/`date.today(` даёт восемь кандидатов в библиотечном коде без
инъекции (`scripts/lib/claim_task.py:362`, `scripts/measure/do_rows_read.py:375`,
`scripts/measure/quotas.py:153,281,336`, `scripts/orchestra/repo_invariants.py:932`,
`scripts/orchestra/scheduler.py:3405`, `scripts/orchestra/waiting_owner_guard.py:367`,
`scripts/review/ai_review.py:700`) — но не отличает опасный паттерн (сравнение
абсолютной даты с системным порогом) от безопасного (`datetime.now() -
timedelta(...)`, сдвиго-инвариантен). Различение требует dataflow-анализа,
которого статический скан не делает.

## Решение

Прогон всего набора тестов (`python -m pytest scripts/`) с системным «сейчас»,
сдвинутым вперёд на несколько горизонтов — сдвиго-инвариантные тесты остаются
зелёными, бомбы падают сразу, включая то, что grep не видит. Механизм сдвига —
**freezegun**, не **libfaketime**:

| | freezegun | libfaketime |
|---|---|---|
| Уровень патча | Python-объекты (`datetime.datetime`, `time.time`, `date.date`) — переписывает и уже импортированные `from datetime import datetime` в модулях (ровно паттерн этого репозитория) | `LD_PRELOAD` поверх libc (`clock_gettime`) — системный уровень |
| Платформа | Чистый Python, кроссплатформенно | Linux-only, требует `apt-get install libfaketime` — не гарантирован на `ubuntu-latest` без отдельного шага установки |
| Область действия | Только процесс pytest | Любой процесс под `LD_PRELOAD`, включая непредсказуемые сайд-эффекты (git-подпроцессы теста тоже увидят сдвинутое время — не всегда желательно) |
| Плавность хода времени | `tick=True` — «сейчас» продолжает идти вперёд от сдвинутой точки нативно | Требует отдельной обвязки для того же эффекта |
| Стоимость подключения | `pip install freezegun`, зависимость только тестового прогона | Системный пакет + возможен sudo-шаг в CI |

Обычный прогон (`pytest scripts/ -q` без `CLOCK_SHIFT_DAYS`) не импортирует
freezegun вовсе (см. `scripts/conftest.py`) — новая зависимость не течёт в
обязательный гейт `test`, только в отдельный периодический
`clock-shift-tests.yml`.

## Честная граница

freezegun меняет часы ТОЛЬКО процесса-интерпретатора pytest. Время, которое
читает не сам Python-код теста, а внешняя программа через `subprocess`
(`git commit`, mtime файла, часы самого раннера GitHub в логах) — не сдвигается.
Если бомба живёт в сравнении с git-таймстампом или mtime, этот носитель её не
поймает; статический аудит таких мест (если появятся) остаётся отдельной
задачей.

## Последствия

- Новая зависимость тестового прогона: `freezegun` (только в
  `clock-shift-tests.yml`, не в `repo-ci.yml`).
- Класс «тест зависит от настенных часов» ловится механически на горизонтах
  +1/+8/+40/+400 дней (обоснование чисел — докстринг
  `scripts/measure/clock_shift_suite.py`), не оставлен на усмотрение ревью.
- Потребитель красного — `pulse_guard.py::WATCHED_WORKFLOWS`
  (`failure_watch`), тот же канал, что у остальных наблюдаемых workflow.
