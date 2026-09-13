"""Носитель на класс «тест зависит от настенных часов и покраснеет в будущем»
(issue #649). Разбор кода не различает опасный паттерн (сравнение абсолютной
даты фикстуры с порогом, который код меряет от системного времени) от
безопасного (`datetime.now() - timedelta(...)`, сдвиго-инвариантен) — только
dataflow. Сдвиг часов различает бесплатно: сдвиго-инвариантные тесты остаются
зелёными, бомбы падают. Живой случай, оплативший это решение (2026-09-07):
`scripts/measure/test_dispatch_tail.py::test_dispatch_failure_writes_note_not_row`
истёк по фикстуре 2026-08-31 против `MAX_CAMPAIGN_DAYS=7` и стал перманентно
красным в обязательном job'е `test` — блокировал слияние 33 открытых PR
(задача #643, PR #646).

Выбор механизма — freezegun, не libfaketime:
  - freezegun патчит `datetime.datetime`/`date.date`/`time.time` на уровне
    Python-объектов (в т.ч. уже импортированные `from datetime import
    datetime` в модулях вида scripts/lib/claim_task.py) — ровно паттерн этого
    репозитория (`datetime.now(timezone.utc)` без инъекции). libfaketime
    работает через LD_PRELOAD поверх libc (`clock_gettime`) — Linux-only,
    требует системный пакет (`apt-get install libfaketime`, не гарантирован
    на ubuntu-latest без sudo-шага), сдвигает время ВСЕМ процессам разом
    (сложнее держать «обычный прогон не должен ничего заметить» — CLOCK_SHIFT
    ниже выключен по умолчанию нулём) и не даёт `tick=True` (плавное
    продолжение хода времени от сдвинутой точки, не остановленный кадр) без
    отдельной обвязки. freezegun — чистый Python, зависимость только
    тестового прогона (`pip install freezegun`), поведение точно
    воспроизводимо локально и в CI.
  - `tick=True`: часы не замирают в одной точке, а продолжают идти вперёд от
    сдвинутого «сейчас» — тесты, меряющие относительные интервалы (retry,
    backoff, «прошло N секунд»), не ломаются сдвигом самим по себе.

Покрытие: freezegun перехватывает ЛЮБОЙ прямой `datetime.now()`/`utcnow()`/
`time.time()`/`date.today()` — и уже инъекционные точки (`now_utc()` в
dispatch_tail.py, `now = now or datetime.now(...)` в claim_task.py/
scheduler.py — тоже читают datetime.now() внутри, freezegun сдвигает и их),
и прямые чтения без инъекции (по замеру issue #649, восемь мест:
scripts/lib/claim_task.py:362, scripts/measure/do_rows_read.py:375,
scripts/measure/quotas.py:153,281,336, scripts/orchestra/repo_invariants.py:932,
scripts/orchestra/scheduler.py:3405, scripts/orchestra/waiting_owner_guard.py:367,
scripts/review/ai_review.py:700 — числа строк дрейфуют с правками файлов,
это не второе место правды, а срез на момент замера; scripts/measure/
dispatch_tail.py:503,596 читают `time.time()` напрямую для меток времени
CSV, тоже сдвигаются freezegun'ом).
НЕ покрывает: время, которое читает не Python-процесс теста, а внешняя
программа, вызванная через subprocess (`git commit` без faked времени,
файловые mtime, время самого раннера GitHub в логах) — freezegun меняет
только процесс интерпретатора pytest, не системные часы ОС. Если бомба живёт
в сравнении с git-таймстампом или mtime файла, этот носитель её не поймает.

Включение — только по явному запросу окружения (CLOCK_SHIFT_DAYS), обычный
прогон (`pytest scripts/ -q` без переменной) не трогает время вовсе и не
требует freezegun установленным.

ВАЖНО про момент старта freeze: сдвиг запускается хуком `pytest_configure`
(ДО коллекции тестов), не autouse-фикстурой сессии (ПОСЛЕ коллекции). Разница
не косметическая. 33 из тестовых файлов этого репозитория (весь
scripts/**/test_*.py, что грузит соседний скрипт как модуль — здесь нет
пакетов, только отдельные файлы) используют идиому
`importlib.util.spec_from_file_location(...) → module_from_spec →
exec_module` БЕЗ регистрации в `sys.modules`. freezegun штатно патчит уже
импортированные модули СКАНИРУЯ `sys.modules` в момент `start()` — модуль, не
попавший в `sys.modules`, для этого скана невидим, и его `datetime`/`time`
остаются настоящими НАВСЕГДА (проверено живым экспериментом при разработке
этого носителя, issue #649: `ai_review.py`, загруженный так тестом, сохранял
настоящий `datetime.datetime` при freeze, стартовавшем ПОСЛЕ его exec_module,
и тест на возраст комментария в `manual_dispatch_skip_reason` ложно
показывал 0 минут — «сейчас» кода было настоящим, а «раньше» теста —
сдвинутым). Если freeze стартовал ДО exec_module (здесь — до коллекции),
свежий `from datetime import datetime` внутри свежеисполняемого модуля
подхватывает уже подменённый класс из самого модуля `datetime` — сканирование
`sys.modules` не нужно вовсе. `pytest_configure` — единственный хук,
гарантированно отрабатывающий раньше импорта любого тестового файла."""

import os

CLOCK_SHIFT_DAYS_ENV = "CLOCK_SHIFT_DAYS"

_freezer = None  # держим ссылку между pytest_configure и pytest_unconfigure


def _shift_days() -> int:
    raw = os.environ.get(CLOCK_SHIFT_DAYS_ENV, "").strip()
    if not raw:
        return 0
    try:
        return int(raw)
    except ValueError as error:
        raise RuntimeError(
            f"{CLOCK_SHIFT_DAYS_ENV}={raw!r} не целое число дней"
        ) from error


def pytest_configure(config):
    """Стартует сдвиг ДО коллекции тестов (см. докстринг модуля — почему это
    не autouse-фикстура). Без CLOCK_SHIFT_DAYS — no-op, freezegun даже не
    импортируется."""
    global _freezer
    days = _shift_days()
    if not days:
        return

    try:
        from freezegun import freeze_time
    except ImportError as error:
        raise RuntimeError(
            f"{CLOCK_SHIFT_DAYS_ENV}={days} задан, но freezegun не установлен "
            "(pip install freezegun) — сдвиг не может считаться применённым, "
            "прогон обязан упасть громко, а не тихо идти по настоящему времени"
        ) from error

    from datetime import datetime, timedelta, timezone

    target = datetime.now(timezone.utc) + timedelta(days=days)
    print(f"::notice::CLOCK_SHIFT_DAYS={days} — часы сдвинуты на {target.isoformat()}")
    _freezer = freeze_time(target, tick=True)
    _freezer.start()


def pytest_unconfigure(config):
    global _freezer
    if _freezer is not None:
        _freezer.stop()
        _freezer = None
