#!/usr/bin/env python3
"""Непрерывный сторож квот харнеса (#605, продолжение #324/#454).

## Повод и числа

2026-09-06 суточная квота DO rows_read была превышена в полтора раза
(7 487 640 при лимите 5 000 000, #324/quotas.py) — прод отдавал HTTP 500,
воркеры конвейера падали с «Морда недоступна», встала и доработка PR,
который эту же квоту чинит. Обнаружено НЕ автоматикой: `quotas.yml` до этой
правки имел только `workflow_dispatch` — снятие среза было by-demand
операцией, сработавшей случайно спустя часы после отказа.

Профиль расхода того дня: 4 001 276 rows_read за 09-05 (80.0% от лимита),
7 487 640 за 09-06 (149.75%), пиковый час 18:00 UTC дал 2 848 898 —
**56.98% суточного лимита за один час**. При таком темпе от 0% до порога
80% (4 000 000) уходит 4 000 000 / 2 848 898 × 60 ≈ **84 минуты**, а от 80%
до 100% (5 000 000, жёсткий потолок) — ещё 5 000 000 / 2 848 898 × 60 ≈
**105 минут общих**, то есть окно между «порог пробит» и «квота исчерпана»
на пиковом темпе — **≈21 минута**. Часовая или более редкая проверка
физически не успевает среагировать внутри этого окна (ровно то, что
произошло 2026-09-06). Отсюда — интервал 15 минут: тот же порядок, что и
у остальных пульсов конвейера (`orchestra.yml` cron `*/15`,
`pulse_guard.HEARTBEAT_MAX_AGE_MINUTES = 45` = 3×15) — даёт в среднем
~1.4 проверки внутри 21-минутного окна риска, то есть реальный шанс поймать
переход, а не только его последствия.

## Почему `schedule` — не единственный носитель

Измерено на этом репозитории (`docs/research/21-github-actions.md`,
«Замер schedule на этом репозитории»): cron `*/15` за 116.3 ч дал **31 из
~465** ожидаемых тиков (**6.7%**), интервалы между доставками 1.9–7.8 ч —
ровно тот «рывками раз в пару часов» опыт, о котором сказано в задаче.
Dispatch-события (`repository_dispatch`/`workflow_dispatch`) за то же окно
доставлены **32 из 32**. Поэтому `schedule` здесь — только страховка
«тихих часов» (когда в репозитории вообще нет активности), а НЕ основной
носитель: основной — событие `pull_request` (`.github/workflows/
quota-watch.yml`, types opened/synchronize/reopened/labeled), которое в
этом репозитории с его непрерывной многоагентной работой (десятки открытых
PR, синхронизация на каждый пуш) срабатывает практически постоянно и
доставляется как обычный webhook, не как `schedule` (тот же класс
надёжности, что измеренные 32/32 dispatch-события, не 6.7% cron).

## Стоимость и троттлинг

Дешёвая проверка — ОДНА метрика (`do_rows_read.today_rows_read`, ~9
GraphQL-запросов: интроспекция схемы, см. докстринг do_rows_read.py, —
дороже одного запроса данных, но вдвое дешевле полного среза quotas.py,
который вдобавок опрашивает workers requests/DO storage/DO written и 4
GitHub REST эндпоинта). Cloudflare GraphQL Analytics — метрический API,
ОТДЕЛЬНЫЙ от метрируемого ресурса (чтение аналитики не расходует DO
rows_read/Workers requests квоты сама по себе) — сторож не приближает то,
от чего защищает.

Троттлинг (`recent_run_within`) ограничивает частоту дорогого CF-запроса
15 минутами НЕЗАВИСИМО от того, как часто реально стреляет `pull_request`
(непредсказуемо — от нуля до нескольких синхронизаций в минуту, при
условии, что каждый такой прогон СТАРТУЕТ уже после завершения
предыдущего — см. оговорку в разделе «Эскалация и автозадача» ниже про
одновременные прогоны): проверка истории прогонов этого же workflow — один
дешёвый GitHub REST вызов (не CF), не требующий отдельного маркера-«тика»
в #120 (не плодим комментарии на каждый холостой пуш). Худший случай — 96
дешёвых проверок/сутки (~9 GraphQL-запросов каждая, ~864/сутки) плюс ДО 24
полных срезов/сутки (не чаще раза в час — `full_sweep` дополнительно
требует `now.minute < FULL_SWEEP_MINUTE_WINDOW`). «Не чаще раза в час» —
верхняя граница, НЕ гарантия нижней: попадание в 15-минутное окно каждого
часа зависит от того, стартовал ли в нём хоть один прогон `pull_request`
(в этом активном репозитории — обычно да), а прогон, завершившийся за 15
минут до начала окна, троттлингом уносит шанс на срез в этом часу целиком —
единственный по-настоящему гарантированный тик внутри окна даёт cron
workflow-файла (`quota-watch.yml`: `3,18,33,48 * * * *`, минута `:03`
каждого часа лежит внутри `[0, 15)`). Худший случай по стоимости — на два
порядка меньше документированного лимита Cloudflare API (1200 запросов/5
минут на токен).

## Эскалация и автозадача

Общий канал `scripts/measure/quota_alert.py` — дедуп по переходу состояния
(issue #120) + заведение/дополнение задачи через `scripts/gh/issue-create`
с меткой `area:process` (наивысший приоритет выбора, #361), а не
констатация в лог. Второй канал алертов не заводится: и дешёвая, и полная
проверки зовут ОДНУ и ту же функцию `quota_alert.check_and_alert` по
ОДНОМУ и тому же ключу ресурса (`cf_do_rows_read_day` для rows_read у
обеих) — что бы ни увидело метрику первым, второе увидит уже известное
состояние и не продублирует сигнал.

Оговорка про троттлинг выше (found: ревью PR #607) — `recent_run_within`
защищает от последовательных прогонов, НЕ от одновременных: несколько
`pull_request`-прогонов, стартовавших ДО того, как первый из них успел
завершиться и попасть в историю workflow, каждый пройдёт троттлинг (все
видят «прошлого завершённого прогона моложе 15 минут нет») и каждый
позовёт Cloudflare — цена от этого не страдает (метрики на порядки ниже
лимита CF API), но на самом переходе ok→breach два таких параллельных
прогона могут оба прочитать «ok» до того, как любой из них запишет маркер
breach: гвардия дублей `scripts/lib/duplicate_guard.py` (#566) погасит
вторую попытку `create_or_note_task` (не заведёт вторую задачу), а вот
второе Telegram-сообщение и второй след в #120 — нет, они уходят раньше,
чем гвардия успевает сработать. Троттлинг здесь — best-effort снижение
частоты дорогого запроса, не жёсткий инвариант «ровно один сигнал на
переход».

Запуск тестов: python -m pytest scripts/measure/test_quota_watch.py -q
"""

from __future__ import annotations

import importlib.util
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_PG_PATH = Path(__file__).resolve().parents[1] / "orchestra" / "pulse_guard.py"
_pg_spec = importlib.util.spec_from_file_location("pulse_guard", _PG_PATH)
pulse_guard = importlib.util.module_from_spec(_pg_spec)
_pg_spec.loader.exec_module(pulse_guard)  # type: ignore[union-attr]

_QA_PATH = Path(__file__).with_name("quota_alert.py")
_qa_spec = importlib.util.spec_from_file_location("quota_alert", _QA_PATH)
quota_alert = importlib.util.module_from_spec(_qa_spec)
_qa_spec.loader.exec_module(quota_alert)  # type: ignore[union-attr]

_DRR_PATH = Path(__file__).with_name("do_rows_read.py")
_drr_spec = importlib.util.spec_from_file_location("do_rows_read", _DRR_PATH)
do_rows_read = importlib.util.module_from_spec(_drr_spec)
_drr_spec.loader.exec_module(do_rows_read)  # type: ignore[union-attr]

_QZ_PATH = Path(__file__).with_name("quotas.py")
_qz_spec = importlib.util.spec_from_file_location("quotas", _QZ_PATH)
quotas = importlib.util.module_from_spec(_qz_spec)
_qz_spec.loader.exec_module(quotas)  # type: ignore[union-attr]

# Обоснование числа — докстринг модуля, раздел «Повод и числа».
CHECK_INTERVAL_MINUTES = 15.0

# Полный срез (quotas.py, все CF+GH метрики) — редкий и дорогой; идёт только
# внутри первой четверти часа, то есть максимум раз в час (см. докстринг,
# «Стоимость и троттлинг»).
FULL_SWEEP_MINUTE_WINDOW = 15

WORKFLOW_FILE = "quota-watch.yml"

# Ключ ресурса rows_read — ОДИН и тот же в дешёвой проверке ниже и в
# quotas.LIMITS (full_sweep) — общий ключ дедупа quota_alert (см. докстринг
# модуля, «Эскалация и автозадача»): дублирующий алерт на один и тот же
# ресурс из двух путей не заводится по построению, не по договорённости.
ROWS_READ_KEY = "cf_do_rows_read_day"
ROWS_READ_LABEL = "DO rows_read/сутки"

# Соответствие «отображаемое имя ресурса quotas.py» → «ключ quotas.LIMITS»
# для full_sweep — сопоставление по подстроке, не по точному равенству:
# GH-строки несут переменный поясняющий хвост в скобках (см. quotas.py::
# collect_github), точное сравнение молча переставало бы находить ключ при
# любой правке текста пояснения.
_RESOURCE_KEY_HINTS = (
    (ROWS_READ_LABEL, ROWS_READ_KEY),
    ("DO rows_written/сутки", "cf_do_rows_written_day"),
    ("Workers requests/сутки", "cf_workers_requests_day"),
    ("DO storage/аккаунт", "cf_do_storage_account_bytes"),
    ("Диспатчи этого репо/час", "gh_dispatch_hour"),
    ("In-progress workflow runs", "gh_concurrent_jobs"),
)


def resource_key(resource_label: str) -> str | None:
    for hint, key in _RESOURCE_KEY_HINTS:
        if hint in resource_label:
            return key
    return None


def recent_run_within(repo: str, workflow_file: str, minutes: float) -> bool:
    """True — у workflow_file уже есть ЗАВЕРШЁННЫЙ прогон моложе `minutes`.
    Троттлинг дорогого CF-запроса, не зависящий от частоты реального
    триггера (см. докстринг модуля). Сбой самой проверки — не повод молчать
    о квоте: неудача трактуется как «не троттлим», прогон продолжается."""
    try:
        data = pulse_guard.gh(
            "--method", "GET", f"repos/{repo}/actions/workflows/{workflow_file}/runs",
            "-f", "status=completed", "-f", "per_page=1",
        )
    except RuntimeError as error:
        print(f"::warning::quota_watch: история прогонов {workflow_file} недоступна "
              f"({error}) — троттлинг пропущен, проверка продолжается", file=sys.stderr)
        return False
    runs = (data or {}).get("workflow_runs") or []
    if not runs:
        return False
    last = pulse_guard.parse_time(runs[0]["created_at"])
    return pulse_guard.minutes_between(last, datetime.now(timezone.utc)) < minutes


def cheap_check(repo: str, token: str, account_id: str) -> str:
    """Одна метрика — rows_read, та самая, что вызвала инцидент #324. Не
    ловит RuntimeError интроспекции/сети наружу — сбой печатается и
    считается «нет данных в этом прогоне», не падением всего сторожа."""
    try:
        current = do_rows_read.today_rows_read(token, account_id)
    except RuntimeError as error:
        print(f"::warning::quota_watch: дешёвый замер rows_read не удался: {error}")
        return ""
    limit = do_rows_read.DAILY_LIMIT
    pct = round(100.0 * current / limit, 1)
    print(f"{ROWS_READ_LABEL}: {current:,} / {limit:,} ({pct}%)".replace(",", " "))
    # Порог — ОДНО место правды (quotas.THRESHOLD_PCT), не второй независимый
    # литерал: дрейф одного дал бы разную чувствительность ручного среза и
    # непрерывного сторожа (found: ревью PR #607).
    return quota_alert.check_and_alert(repo, ROWS_READ_KEY, ROWS_READ_LABEL, current, limit, pct,
                                        threshold=quotas.THRESHOLD_PCT)


def full_sweep(repo: str, token: str, account_id: str) -> list[str]:
    """Полный срез (все CF+GH метрики, quotas.py) — редкий и дорогой путь,
    вызывается не чаще раза в час (см. FULL_SWEEP_MINUTE_WINDOW). rows_read
    здесь тоже присутствует — тот же resource_key, что у cheap_check, дедуп
    quota_alert не даст продублировать алерт на неё."""
    if not (token and account_id):
        return []
    results = []
    for row in quotas.collect_cloudflare(account_id, token) + quotas.collect_github(repo):
        key = resource_key(row.resource)
        if key is None or row.pct is None:
            continue
        result = quota_alert.check_and_alert(repo, key, row.resource, row.current, row.limit, row.pct,
                                              threshold=quotas.THRESHOLD_PCT)
        print(f"[полный срез] {result}")
        results.append(result)
    return results


def _channel_failed(result: str) -> bool:
    # Тот же критерий, что quotas.py::main — оба канала эскалации молчат.
    return "НЕ доставлен" in result and "НЕ оставлен" in result


def main() -> int:
    repo = os.environ.get("GITHUB_REPOSITORY", "mytab0r/edge-harness")
    now = datetime.now(timezone.utc)
    token = os.environ.get("CLOUDFLARE_API_TOKEN")
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID")

    if not token or not account_id:
        print("quota_watch: CLOUDFLARE_API_TOKEN/CLOUDFLARE_ACCOUNT_ID не заданы — "
              "наблюдение невозможно в этом прогоне")
        return 0

    if recent_run_within(repo, WORKFLOW_FILE, CHECK_INTERVAL_MINUTES):
        print(f"quota_watch: прошлый прогон моложе {CHECK_INTERVAL_MINUTES} мин — "
              "пропуск (троттлинг частоты CF-запроса)")
        return 0

    exit_code = 0
    result = cheap_check(repo, token, account_id)
    if result:
        print(result)
        if _channel_failed(result):
            exit_code = 1

    if now.minute < FULL_SWEEP_MINUTE_WINDOW:
        for r in full_sweep(repo, token, account_id):
            if _channel_failed(r):
                exit_code = 1

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
