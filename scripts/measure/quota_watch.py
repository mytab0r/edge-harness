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

Троттлинг ограничивает частоту дорогого CF-запроса 15 минутами НЕЗАВИСИМО
от того, как часто реально стреляет `pull_request` (непредсказуемо — от
нуля до нескольких синхронизаций в минуту, при условии, что каждый такой
прогон СТАРТУЕТ уже после завершения предыдущего — см. оговорку в разделе
«Эскалация и автозадача» ниже про одновременные прогоны): проверка истории
прогонов этого же workflow — дешёвые GitHub REST вызовы (не CF), не
требующие отдельного маркера-«тика» в #120 (не плодим комментарии на
каждый холостой пуш). Худший случай — 96 дешёвых проверок/сутки (~9
GraphQL-запросов каждая, ~864/сутки) плюс ДО 24 полных срезов/сутки (не
чаще раза в час — `full_sweep` дополнительно требует
`now.minute < FULL_SWEEP_MINUTE_WINDOW`). «Не чаще раза в час» — верхняя
граница, НЕ гарантия нижней: попадание в 15-минутное окно каждого часа
зависит от того, случился ли в нём хоть один РЕАЛЬНЫЙ замер (в этом
активном репозитории — обычно да), а замер, случившийся за 15 минут до
начала окна, троттлингом уносит шанс на срез в этом часу целиком —
единственный по-настоящему гарантированный тик внутри окна даёт cron
workflow-файла (`quota-watch.yml`: `3,18,33,48 * * * *`, минута `:03`
каждого часа лежит внутри `[0, 15)`). Худший случай по стоимости — на два
порядка меньше документированного лимита Cloudflare API (1200 запросов/5
минут на токен).

### Троттлинг: только реальный замер открывает окно (found: ревью PR #607)

До этой правки `recent_run_within` смотрел ТОЛЬКО на факт «есть ЗАВЕРШЁННЫЙ
прогон `quota-watch.yml` моложе 15 минут» — `status=completed` покрывает и
`failure` (упал раньше вызова Cloudflare — например, шаг тестов), и
холостой прогон (гейт САМ решил не измерять). При «практически постоянной»
активности репозитория (основной носитель каденции, см. выше) КАЖДЫЙ
следующий тик видел свежий завершённый прогон и пропускал замер — сторож
слеп именно в активные часы, оставаясь ЗЕЛЁНЫМ, а строку «пропуск
(троттлинг)» никто не читает. Хуже: в день инцидента, когда конвейер
красный, упавших прогонов больше, и слепота гарантирована.

Правка: workflow разведён на два шага в одном job'е — `GATE_STEP_NAME`
(гейт, всегда выполняется, решает и пишет `proceed` в `$GITHUB_OUTPUT`) и
`MEASURE_STEP_NAME` (замер, `if: steps.gate.outputs.proceed == 'true'`).
Шаг замера, которого гейт не пустил, получает `conclusion: skipped` в
GitHub Jobs API — единственный надёжный признак «замера не было» видимый
СНАРУЖИ прогона, не изнутри Python-процесса. `last_real_measurement_age_minutes`
(единая точка входа и для троттлинга в `gate_main`, и для проверки простоя
замера ниже) сканирует последние завершённые прогоны от новых к старым, для
каждого читает jobs/steps и
считает «замером» только `MEASURE_STEP_NAME` с `conclusion` in
(`success`, `failure`) — ЛЮБОЙ реальный запуск шага, а не только успешный:
шаг, упавший ПОСЛЕ вызова Cloudflare (например, канал эскалации не
доставил breach), тоже потратил CF-запрос и обязан считаться троттлингом,
иначе следующий тик ударил бы по Cloudflare второй раз впустую. Сканирование
останавливается, как только встречен прогон старше окна (дальше все ещё
старше — ответ уже известен: «замера в окне не было»), поэтому число
REST-вызовов ограничено окном, а не общим числом прогонов.

## Эскалация и автозадача

Общий канал `scripts/measure/quota_alert.py` — дедуп по переходу состояния
(issue #120) + заведение/дополнение задачи через `scripts/gh/issue-create`
с меткой `area:process` (наивысший приоритет выбора, #361), а не
констатация в лог. Второй канал алертов не заводится: и дешёвая, и полная
проверки зовут ОДНУ и ту же функцию `quota_alert.check_and_alert` по
ОДНОМУ и тому же ключу ресурса (`cf_do_rows_read_day` для rows_read у
обеих) — что бы ни увидело метрику первым, второе увидит уже известное
состояние и не продублирует сигнал.

Оговорка про троттлинг выше (found: ревью PR #607) — `last_real_measurement_age_minutes`
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

## Простой замера — громкий сигнал, не тихий сток (found: ревью PR #607)

Требование сверх фикса троттлинга: сторож обязан ГРОМКО сообщать, если
реальный замер не выполнялся дольше порога — молчаливый пропуск это ровно
тот класс дефекта, который и так уже встречался («механизм зеленеет, когда
сам не работает»). Канал — ТОТ ЖЕ `pulse_guard.escalate` (Telegram + след в
issue #120), что и у остальных предохранителей репозитория, а не
`GITHUB_STEP_SUMMARY` (тот сток не доставляется человеку — по этому классу
уже заведена отдельная задача #637, здесь не решается).

`gate_main()` при КАЖДОМ прогоне вызывает `last_real_measurement_age_minutes`
с окном `HISTORY_LOOKBACK_MINUTES` (не `MEASUREMENT_STALE_MINUTES` — см.
следующий раздел, «Простой vs холодный старт», found: PR #607 разбудил
владельца ложным сигналом). Дедуп эскалации — эпизодный, тот же приём
(`STALE_MARKER`/`STALE_RESOLVED_MARKER` + `pulse_guard.episode_reopened`),
что `pulse_guard.heartbeat_check` уже использует для
`HEARTBEAT_NO_TICKS_MARKER`/`HEARTBEAT_TICKS_RESUMED_MARKER` — один сигнал
на эпизод простоя, не на каждые 15 минут, пока эпизод держится; явное
закрытие эпизода (`close_stale_episode_if_needed`), как только замер снова
нашёлся, иначе следующий настоящий простой останется заглушен старым
маркером (тот же класс, что уже закрыт в pulse_guard.py). Сбой самой
проверки истории (сеть/права) НЕ трактуется как «простой» — ложный алерт на
транзитный сбой инструмента был бы хуже пропуска: сигнал в этом случае
только предупреждение в лог прогона, а не эскалация.

### Простой vs холодный старт, и неслитый код не будит владельца (found: ревью PR #607)

PR #607 сам поймал живой дефект своего же первого варианта: свежеоткрытый
PR (ветка `agent/605-quota-continuous-watch`, workflow ещё НЕ на `main`)
разбудил владельца в Telegram сообщением, которое гадало о причине —
«гейт троттлит без причины, workflow не запускается вовсе, либо шаг замера
падает раньше вызова Cloudflare». Три отдельных дефекта, три отдельных фикса:

1. **«Простаивает» ≠ «ни разу не запускался».** Старая проверка звала
   `last_real_measurement_age_minutes` с окном `MEASUREMENT_STALE_MINUTES`
   (45 мин) и трактовала ЛЮБОЙ `age is None` (реального замера в окне не
   найдено) как простой — в том числе на САМОМ ПЕРВОМ прогоне НОВОГО
   workflow, у которого просто ещё нет истории. Правильная семантика:
   тревожить на ПЕРЕХОДЕ «измерял → перестал», не на отсутствии замера
   вообще (тот же принцип, что `quota_alert.check_and_alert` уже применяет к
   переходу ok/breach — первое наблюдение, заставшее ресурс уже в норме, не
   генерирует ложное «восстановилось»). Реализация: `gate_main` зовёт
   `last_real_measurement_age_minutes` с окном `HISTORY_LOOKBACK_MINUTES`
   (7 дней, практически «вся видимая история» — реальный потолок всё равно
   даёт `MEASUREMENT_SCAN_PER_PAGE`, одна страница прогонов) вместо 45 минут,
   и различает ТРИ исхода по одному и тому же `age`:
   - `age is None` — реального замера не было НИ РАЗУ в видимой истории —
     ХОЛОДНЫЙ СТАРТ, не простой. Решение — МОЛЧАТЬ (не отдельное сообщение):
     заводить третий канал/маркер под редкий и уже покрытый CI-тестом класс
     («имена шагов разъехались» ловит `test_workflow_step_names_match_
     constants` ДО мержа) избыточно по цене (Меньше кода); отдельно, гейт
     сам работает без `if:` и обязан выполниться в КАЖДОМ прогоне — если
     когда-нибудь мониторинг действительно не заработает НИ РАЗУ за 7 дней
     видимой истории на `main`, это уже не «холодный старт», а `age`
     перестаёт быть None (см. следующий пункт) и сигнал уйдёт как обычный
     переход. Единственный процентно-непокрытый остаток — гипотетический
     «замер никогда не проходил вообще, даже один раз, дольше 7 дней при
     непрерывной активности репозитория» — не проверено живым инцидентом,
     честно вынесено в раздел «Осталось непроверенным» тестового файла.
   - `age < MEASUREMENT_STALE_MINUTES` — замер свежий, троттлинг делает своё
     дело, `close_stale_episode_if_needed` закрывает эпизод, если он был
     открыт.
   - `age >= MEASUREMENT_STALE_MINUTES` — замер БЫЛ (найден где-то в
     видимой истории), но давно — настоящий ПЕРЕХОД «измерял → перестал».
     Именно этот и только этот случай — кандидат на эскалацию.
2. **Неслитый код не может разбудить владельца — но три состояния, не два
   (found: доп. цикл ревью PR #607).** Кандидат на эскалацию (переход выше)
   проверяется через `workflow_version_check`, которая различает ТРИ исхода
   сравнения исполняемой ПРЯМО СЕЙЧАС копии `.github/workflows/
   quota-watch.yml` с версией на `main` (Contents API), а не два:
   - **совпадает** — эскалация уходит как обычно.
   - **достоверно отличается** (Contents API ОТВЕТИЛ: либо файла на `main`
     вовсе нет — PR не слит; либо содержимое не совпало — PR правит именно
     этот файл) — эскалация подавляется. Это и есть защита «неслитый код не
     может разбудить владельца».
   - **проверить не удалось** (сеть/права/таймаут запроса к Contents API —
     запрос НЕ дал ответа вовсе) — эскалация УХОДИТ, с честной припиской в
     тексте, что версию подтвердить не удалось и почему. Первая версия этой
     правки (PR #607) сливала этот исход с «достоверно отличается» —
     `except RuntimeError` возвращал тот же консервативный `False`, что и
     ветка несовпадения содержимого. Дефект: сетевая заминка ИМЕННО в
     момент, когда замер квоты действительно встал, гасила эскалацию —
     сторож, который замолкает при собственном сбое, хуже отсутствия
     сторожа. Выбор в пользу тревоги, а не тишины: ложный пейдж в этом
     состоянии требует совпадения ТРЁХ редких условий сразу — сбой сети И
     простой замера И неслитая версия, — тогда как молчание наступает от
     ОДНОГО условия (сбой сети). Правило репозитория «алерт не гадает»
     (AGENTS.md, см. следующий пункт) тоже требует прямо называть, чего
     установить не удалось, а не тихо подавлять сигнал.

   Почему не проверка `github.event.pull_request.head.repo.full_name !=
   github.repository` (форк): живой инцидент был прогоном ИЗ ВЕТКИ ТОГО ЖЕ
   репозитория, не форка — эта проверка его не поймала бы вовсе. Почему не
   отключение `pull_request` как триггера: он остаётся ОСНОВНЫМ носителем
   каденции ПОСЛЕ слияния (см. «Почему `schedule` — не единственный
   носитель» выше) — вопрос не «откуда пришёл прогон», а «та ли версия файла
   сейчас исполняется, что лежит в `main`». Гвардия САМА перестаёт что-либо
   блокировать в момент мержа (тогда локальная копия и `main` совпадают
   побайтово по построению) и остаётся живой для ЛЮБОГО следующего PR,
   вновь коснувшегося этого файла — не разовый костыль под один PR #607.
3. **Алерт не гадает (AGENTS.md).** Раньше текст перечислял три гипотезы
   через «либо». Тот же ответ GitHub Jobs API, что уже отличает
   success/failure/skipped, различает причину САМ: `_classify_measurement_
   absence` смотрит на шаг `MEASURE_STEP_NAME` самого свежего завершённого
   прогона и называет ФАКТ — «прогонов не найдено вовсе», «шаг пропущен
   (гейт не пустил)», «шаг упал (failure) до вызова Cloudflare», либо, если
   даже это узнать не удалось (сеть/права), честно — «причину установить
   нельзя, потому что …» — а не подменяет недостающий факт гипотезой. Тот же
   принцип теперь применён и к `workflow_version_check` выше: состояние
   «проверить не удалось» несёт причину дословно из ошибки, а не гипотезу.

Запуск тестов: python -m pytest scripts/measure/test_quota_watch.py -q
"""

from __future__ import annotations

import base64
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

# Имена шагов workflow (`.github/workflows/quota-watch.yml`) — ТОЧНОЕ
# совпадение обязательно: last_real_measurement_age_minutes ищет их в ответе
# GitHub Jobs API по имени (REST не отдаёт YAML `id:` шага, только `name`).
# Синхронность с YAML проверяет
# test_quota_watch.py::test_workflow_step_names_match_constants (found: ревью
# PR #607) — иначе правка текста в одном файле молча ослепляет распознавание
# «замер состоялся» в другом.
GATE_STEP_NAME = "Гейт: нужен ли реальный замер"
MEASURE_STEP_NAME = "Замер квоты (Cloudflare)"

# Сколько последних завершённых прогонов просмотреть в поисках реального
# замера — верхняя граница на число REST-вызовов при аномально частых
# прогонах; сканирование всё равно останавливается раньше по возрасту
# прогона (см. last_real_measurement_age_minutes), это доп. потолок.
MEASUREMENT_SCAN_PER_PAGE = 30

# Порог «замер простаивал» — 3×CHECK_INTERVAL_MINUTES, тот же приём
# кратности, что pulse_guard.HEARTBEAT_MAX_AGE_MINUTES у пульса
# оркестратора (см. докстринг модуля, «Простой замера»).
MEASUREMENT_STALE_MINUTES = CHECK_INTERVAL_MINUTES * 3

# Верхняя граница «видимой истории» для last_real_measurement_age_minutes в
# gate_main — различает холодный старт (замера не было НИКОГДА) от настоящего
# простоя (замер БЫЛ, потом прекратился), см. докстринг модуля «Простой vs
# холодный старт» (found: ревью PR #607). Не буквальная бесконечность:
# реальный потолок сканирования всё равно даёт MEASUREMENT_SCAN_PER_PAGE
# (одна страница прогонов, без постраничного обхода) — 7 дней просто
# гарантируют, что цикл не остановится РАНЬШЕ по возрасту прогона, не найдя
# достаточно старый прогон в пределах этой страницы.
HISTORY_LOOKBACK_MINUTES = 60.0 * 24 * 7  # 7 дней

STALE_MARKER = "[quota: замер квоты простаивает]"
STALE_RESOLVED_MARKER = "[quota: замер квоты возобновился]"

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


def _find_step_conclusion(jobs_payload: dict | None, step_name: str) -> str | None:
    """Conclusion шага `step_name` среди всех job'ов прогона — None, если шаг
    не найден вовсе (прогон старее, чем эта правка workflow, либо job не
    добежал до создания шага)."""
    for job in (jobs_payload or {}).get("jobs", []) or []:
        for step in job.get("steps", []) or []:
            if step.get("name") == step_name:
                return step.get("conclusion")
    return None


def last_real_measurement_age_minutes(
    repo: str, workflow_file: str, step_name: str, now: datetime, lookback_minutes: float,
) -> tuple[float | None, bool]:
    """Возраст (мин) самого свежего прогона, чей шаг `step_name` РЕАЛЬНО
    выполнился (conclusion `success` или `failure` — не `skipped`, не
    отсутствует) — единственный надёжный признак «CF-замер состоялся»,
    видимый СНАРУЖИ прогона (found: ревью PR #607, см. докстринг модуля,
    «Троттлинг: только реальный замер открывает окно»).

    Возвращает (age_minutes, api_ok):
      - api_ok=False — история прогонов/шагов недоступна (сеть, права).
        Вызывающий обязан трактовать это как «не троттлим» (безопасный
        дефолт — лишний CF-запрос дёшев), но НЕ как «замер простаивал»
        (эскалация станет ложной на транзитном сбое инструмента, а не на
        реальном простое сторожа).
      - age is None при api_ok=True — реального замера НЕ нашлось в пределах
        `lookback_minutes` (либо его не было, либо прогонов вовсе нет).

    Сканирование идёт от новых прогонов к старым и останавливается, как
    только встречен прогон старше `lookback_minutes` — дальше все прогоны
    ещё старше, ответ уже известен."""
    try:
        data = pulse_guard.gh(
            "--method", "GET", f"repos/{repo}/actions/workflows/{workflow_file}/runs",
            "-f", "status=completed", "-f", f"per_page={MEASUREMENT_SCAN_PER_PAGE}",
        )
    except RuntimeError as error:
        print(f"::warning::quota_watch: история прогонов {workflow_file} недоступна "
              f"({error}) — троттлинг и проверка простоя пропущены в этом тике", file=sys.stderr)
        return None, False
    runs = (data or {}).get("workflow_runs") or []
    for run in runs:
        created = pulse_guard.parse_time(run["created_at"])
        age = pulse_guard.minutes_between(created, now)
        if age > lookback_minutes:
            break
        try:
            jobs_payload = pulse_guard.gh(f"repos/{repo}/actions/runs/{run['id']}/jobs?per_page=20")
        except RuntimeError as error:
            print(f"::warning::quota_watch: шаги прогона {run['id']} недоступны ({error}) — "
                  "пропускаю этот прогон, ищу дальше в истории", file=sys.stderr)
            continue
        if _find_step_conclusion(jobs_payload, step_name) in ("success", "failure"):
            return age, True
    return None, True


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
        if key is None:
            # Не всякая строка quotas.py — квота, которую стоит эскалировать
            # (например «LLM-провайдер квота»/«Actions минуты» — намеренно
            # вне _RESOURCE_KEY_HINTS, quotas.py::main их тоже не алертит).
            # Пропуск ГРОМКИЙ (found: ревью PR #607, некритичное замечание):
            # раньше тихий `continue` был неотличим от дрейфа
            # _RESOURCE_KEY_HINTS, молча теряющего реальную квоту.
            print(f"[полный срез] «{row.resource}» — вне списка эскалируемых метрик "
                  "(_RESOURCE_KEY_HINTS), пропуск")
            continue
        if row.pct is None:
            note = f": {row.note}" if row.note else ""
            print(f"[полный срез] «{row.resource}» — нет данных для расчёта % "
                  f"(status={row.status}), пропуск{note}")
            continue
        result = quota_alert.check_and_alert(repo, key, row.resource, row.current, row.limit, row.pct,
                                              threshold=quotas.THRESHOLD_PCT)
        print(f"[полный срез] {result}")
        results.append(result)
    return results


def _channel_failed(result: str) -> bool:
    # Тот же критерий, что quotas.py::main — оба канала эскалации молчат.
    return "НЕ доставлен" in result and "НЕ оставлен" in result


def _write_github_output(name: str, value: str) -> None:
    """Пишет `name=value` в `$GITHUB_OUTPUT` — контракт `steps.gate.outputs.*`,
    который читает `if:` следующего шага в quota-watch.yml. Отсутствие
    GITHUB_OUTPUT (локальный запуск вне Actions) — не падение, только
    громкое предупреждение: гейт не может передать решение дальше, но сам
    остаётся зелёным."""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        print(f"::warning::quota_watch: GITHUB_OUTPUT не задан — вывод {name} не записан",
              file=sys.stderr)
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"{name}={value}\n")


def _local_workflow_path() -> Path:
    """Путь к исполняемой ПРЯМО СЕЙЧАС копии quota-watch.yml — checkout этого
    же прогона (см. workflow_version_check)."""
    return Path(__file__).resolve().parents[2] / ".github" / "workflows" / WORKFLOW_FILE


def _is_not_found_error(error: BaseException) -> bool:
    """404 от Contents API — ДОСТОВЕРНЫЙ факт «файла на `main` нет» (PR не
    слит), а не «сравнить не удалось»: `gh api` (см. `pulse_guard.gh`)
    поднимает RuntimeError при ЛЮБОМ non-2xx, включая 404, — поэтому «файла
    нет» и транзитный сетевой сбой приходят в quota_watch ОДИНАКОВО, обычным
    исключением, без отдельного канала для кода статуса. Различаем по тексту
    самой ошибки: `gh` CLI всегда включает `404` в сообщение при HTTP 404
    (прод-форма: `gh: HTTP 404: Not Found`) — любой другой текст (таймаут,
    DNS, 5xx) не содержит `404` и остаётся состоянием "unknown"."""
    return "404" in str(error)


def workflow_version_check(repo: str) -> tuple[str, str | None]:
    """Три состояния сравнения исполняемой ПРЯМО СЕЙЧАС копии
    `.github/workflows/quota-watch.yml` с версией на `main` (Contents API) —
    НЕ два (found: доп. цикл ревью PR #607, см. докстринг модуля «Неслитый
    код не может разбудить владельца — но три состояния, не два»).

    Возвращает (verdict, note):
      - ("matches", None) — Contents API ОТВЕТИЛ, побайтовое совпадение
        подтверждено. Кандидат на эскалацию уходит как обычно.
      - ("differs", None) — Contents API ОТВЕТИЛ, но несовпадение
        ДОСТОВЕРНО: либо файла на `main` вовсе нет (PR не слит), либо
        содержимое отличается (PR правит именно этот файл). Эскалация
        подавляется — это и есть защита «неслитый код не может разбудить
        владельца».
      - ("unknown", note) — запрос к Contents API НЕ дал ответа вовсе
        (сеть/права/таймаут) — сравнить не удалось, а не «отличается».
        `note` — причина дословно из пойманного исключения. Раньше этот
        исход возвращал тот же `False`/подавление, что и «differs» — сетевая
        заминка ИМЕННО в момент простоя замера гасила эскалацию (сторож,
        замолкающий при своём сбое, хуже отсутствия сторожа). Теперь
        вызывающий (`gate_main`) обязан пропустить эскалацию НЕ подавляя её,
        а дополнив текст фактом «версию подтвердить не удалось» — тревога о
        вставшем замере квоты дороже риска ложного пейджа: ложный пейдж
        здесь требует совпадения ТРЁХ редких условий (сбой сети И простой
        замера И неслитая версия), молчание — ОДНОГО (сбой сети).

    Почему не `github.event.pull_request.head.repo.full_name !=
    github.repository` (сравнение с форком): живой инцидент — прогон ИЗ
    ВЕТКИ ТОГО ЖЕ репозитория (не форк), эта проверка его не поймала бы
    вовсе. Почему не отключение `pull_request` как триггера: он остаётся
    ОСНОВНЫМ носителем каденции ПОСЛЕ слияния (см. докстринг модуля, «Почему
    schedule — не единственный носитель») — вопрос не «откуда пришёл
    прогон», а «та ли версия файла сейчас исполняется, что лежит в main».
    Эта гвардия САМА перестаёт что-либо блокировать в момент мержа (тогда
    локальная копия и main совпадают побайтово по построению) и остаётся
    живой для ЛЮБОГО следующего PR, вновь коснувшегося этого файла — не
    разовый костыль под один PR #607."""
    try:
        payload = pulse_guard.gh(f"repos/{repo}/contents/.github/workflows/{WORKFLOW_FILE}?ref=main")
    except RuntimeError as error:
        if _is_not_found_error(error):
            print(f"::warning::quota_watch: {WORKFLOW_FILE} не найден на main (HTTP 404: {error}) — "
                  "эскалация простоя подавлена (неслитый PR/ветка)", file=sys.stderr)
            return "differs", None
        print(f"::warning::quota_watch: версия {WORKFLOW_FILE} на main недоступна ({error}) — "
              "сравнить не удалось, эскалация простоя УЙДЁТ с честной пометкой об этом (не тишина)",
              file=sys.stderr)
        return "unknown", str(error)
    if not payload or "content" not in payload:
        print(f"::warning::quota_watch: {WORKFLOW_FILE} не найден на main — эскалация простоя "
              "подавлена (неслитый PR/ветка)", file=sys.stderr)
        return "differs", None
    main_bytes = base64.b64decode(payload["content"])
    local_bytes = _local_workflow_path().read_bytes()
    if main_bytes != local_bytes:
        print(f"::notice::quota_watch: исполняемая копия {WORKFLOW_FILE} отличается от версии "
              "на main — эскалация простоя подавлена, пока копии не совпадут", file=sys.stderr)
        return "differs", None
    return "matches", None


def _classify_measurement_absence(repo: str) -> str:
    """Факт, не гипотеза (AGENTS.md, «Алерт не гадает»; found: ревью PR
    #607). Тот же ответ GitHub Jobs API, что уже отличает
    success/failure/skipped, различает причину САМ — по самому свежему
    завершённому прогону: (а) прогонов нет вовсе; (б) шаг замера `skipped`
    (гейт не пустил); (в) шаг замера `failure` (упал до вызова Cloudflare).
    Если даже это узнать не удалось (сеть/права) — честное «причину
    установить нельзя, потому что …», а не подмена гипотезой."""
    try:
        data = pulse_guard.gh(
            "--method", "GET", f"repos/{repo}/actions/workflows/{WORKFLOW_FILE}/runs",
            "-f", "status=completed", "-f", "per_page=1",
        )
    except RuntimeError as error:
        return f"причину установить нельзя — история прогонов {WORKFLOW_FILE} недоступна ({error})"
    runs = (data or {}).get("workflow_runs") or []
    if not runs:
        return f"прогонов {WORKFLOW_FILE} не найдено вовсе"
    run_id = runs[0]["id"]
    try:
        jobs_payload = pulse_guard.gh(f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=20")
    except RuntimeError as error:
        return f"причину установить нельзя — шаги прогона {run_id} недоступны ({error})"
    conclusion = _find_step_conclusion(jobs_payload, MEASURE_STEP_NAME)
    if conclusion == "skipped":
        return (f"самый свежий завершённый прогон пропустил шаг {MEASURE_STEP_NAME!r} — "
                f"гейт {GATE_STEP_NAME!r} не пустил замер")
    if conclusion == "failure":
        return (f"шаг {MEASURE_STEP_NAME!r} самого свежего завершённого прогона упал (failure) "
                "до вызова Cloudflare")
    if conclusion is None:
        return (f"шаг {MEASURE_STEP_NAME!r} не найден в самом свежем завершённом прогоне "
                "(устаревший формат прогона)")
    return (f"шаг {MEASURE_STEP_NAME!r} самого свежего завершённого прогона завершился success, "
            "но вне окна проверки на момент сканирования (гонка между двумя тиками)")


def stale_alert(repo: str, now: datetime, age_minutes: float, reason: str) -> str:
    """Реальный замер БЫЛ (найден `age_minutes` назад где-то в видимой
    истории), но давно — настоящий ПЕРЕХОД «измерял → перестал», не
    холодный старт (см. докстринг модуля «Простой vs холодный старт»,
    found: ревью PR #607). `reason` — уже классифицированный ФАКТ
    (`_classify_measurement_absence`), не гипотеза. Эпизодный дедуп — тот же
    приём, что `pulse_guard.heartbeat_check` для HEARTBEAT_NO_TICKS_MARKER:
    один сигнал на эпизод, не на каждый 15-минутный тик, пока эпизод
    держится."""
    text = (
        f"🚨 edge-harness: {STALE_MARKER}\n"
        f"Реальный замер квоты (шаг {MEASURE_STEP_NAME!r} прогона {WORKFLOW_FILE}) не выполнялся "
        f"{age_minutes:.0f} мин (порог простоя {MEASUREMENT_STALE_MINUTES:.0f} = "
        f"3×{CHECK_INTERVAL_MINUTES:.0f}) — до этого замер РАБОТАЛ, это переход, не холодный старт.\n"
        f"Причина: {reason}."
    )
    try:
        open_times = pulse_guard.issue_marker_times(repo, pulse_guard.WATCHDOG_ISSUE, STALE_MARKER)
        close_times = pulse_guard.issue_marker_times(repo, pulse_guard.WATCHDOG_ISSUE, STALE_RESOLVED_MARKER)
    except RuntimeError as error:
        print(f"::warning::quota_watch: история #{pulse_guard.WATCHDOG_ISSUE} недоступна, "
              f"дедуп эпизода простоя пропущен, сигнал уходит как есть: {error}", file=sys.stderr)
        result = pulse_guard.escalate(repo, pulse_guard.WATCHDOG_ISSUE, text)
        return f"замер простаивал — {result} (дедуп пропущен: история #{pulse_guard.WATCHDOG_ISSUE} недоступна)"
    if not pulse_guard.episode_reopened(open_times, close_times):
        return "замер простаивал — уже сообщено в этом эпизоде (дедуп)"
    result = pulse_guard.escalate(repo, pulse_guard.WATCHDOG_ISSUE, text)
    return f"замер простаивал — {result}"


def close_stale_episode_if_needed(repo: str) -> None:
    """Явное закрытие эпизода простоя, как только замер снова нашёлся — тот
    же приём, что pulse_guard.heartbeat_check делает для
    HEARTBEAT_TICKS_RESUMED_MARKER: без явного закрытия следующий настоящий
    простой останется заглушен старым открывающим маркером эпизода."""
    try:
        open_times = pulse_guard.issue_marker_times(repo, pulse_guard.WATCHDOG_ISSUE, STALE_MARKER)
        if not open_times:
            return
        close_times = pulse_guard.issue_marker_times(repo, pulse_guard.WATCHDOG_ISSUE, STALE_RESOLVED_MARKER)
        if close_times and max(close_times) > max(open_times):
            return
        pulse_guard.post_issue_comment(
            repo, pulse_guard.WATCHDOG_ISSUE,
            f"✅ edge-harness: {STALE_RESOLVED_MARKER}\n"
            "Реальный замер квоты снова найден — эпизод простоя закрыт.",
        )
    except RuntimeError as error:
        print(f"::warning::quota_watch: закрытие эпизода простоя замера не оставлено: {error}",
              file=sys.stderr)


def gate_main() -> int:
    """Шаг-гейт (`GATE_STEP_NAME` в quota-watch.yml): решает, нужен ли
    реальный замер в этом прогоне (троттлинг, `proceed` в $GITHUB_OUTPUT для
    `if:` следующего шага), и отдельно — не простаивал ли реальный замер
    дольше STALE-порога (см. докстринг модуля, «Простой vs холодный старт»).

    Лукбэк для `last_real_measurement_age_minutes` — `HISTORY_LOOKBACK_MINUTES`
    (не `MEASUREMENT_STALE_MINUTES`): на троттлинг (`proceed`) это не влияет
    (`age >= CHECK_INTERVAL_MINUTES` истинно что при 20, что при 200 минутах),
    но даёт различить холодный старт (`age is None` во ВСЕЙ видимой истории)
    от настоящего простоя (`age` найден, просто старше STALE-порога)."""
    repo = os.environ.get("GITHUB_REPOSITORY", "mytab0r/edge-harness")
    now = datetime.now(timezone.utc)
    age, api_ok = last_real_measurement_age_minutes(
        repo, WORKFLOW_FILE, MEASURE_STEP_NAME, now, HISTORY_LOOKBACK_MINUTES,
    )
    proceed = age is None or age >= CHECK_INTERVAL_MINUTES
    _write_github_output("proceed", "true" if proceed else "false")
    if age is None:
        print(f"quota_watch: реального замера ({MEASURE_STEP_NAME!r}) не найдено среди прогонов "
              f"{WORKFLOW_FILE} за всю видимую историю — замер продолжится")
    else:
        verdict = "моложе окна троттлинга — пропуск" if not proceed else "старше окна троттлинга — замер продолжится"
        print(f"quota_watch: последний реальный замер {age:.1f} мин назад ({verdict}, "
              f"порог {CHECK_INTERVAL_MINUTES:.0f} мин)")
    if not api_ok:
        print("::warning::quota_watch: история прогонов недоступна — проверка простоя замера "
              "пропущена в этом тике (троттлинг тоже не сработал — безопасный дефолт)", file=sys.stderr)
        return 0

    if age is None:
        # Замера не было НИ РАЗУ в видимой истории — холодный старт, не
        # простой (см. докстринг модуля, «Простой vs холодный старт»):
        # молчим, а не гадаем и не заводим третий канал сигнала.
        print("quota_watch: холодный старт — реального замера не было НИ РАЗУ в видимой истории, "
              "это не простой (см. quota_watch.py::gate_main); сигнал не уходит")
        return 0
    if age < MEASUREMENT_STALE_MINUTES:
        close_stale_episode_if_needed(repo)
        return 0

    # age >= MEASUREMENT_STALE_MINUTES: замер БЫЛ раньше, но давно —
    # настоящий переход «измерял → перестал». Три исхода проверки версии
    # (см. workflow_version_check, «неслитый код не может разбудить
    # владельца — но три состояния, не два», found: доп. цикл ревью PR
    # #607): "differs" подавляет эскалацию (неслитый код не будит), "unknown"
    # НЕ подавляет — сеть, не «версия отличается», уходит с честной пометкой.
    verdict, note = workflow_version_check(repo)
    if verdict == "differs":
        return 0
    reason = _classify_measurement_absence(repo)
    if verdict == "unknown":
        reason = f"{reason}; кроме того, версию исполняемого workflow подтвердить не удалось: {note}"
    print(stale_alert(repo, now, age, reason))
    return 0


def measure_main() -> int:
    """Шаг замера (`MEASURE_STEP_NAME` в quota-watch.yml, запускается только
    когда гейт выше решил `proceed=true`) — сама дешёвая проверка + (внутри
    первой четверти часа) полный срез. Троттлинг здесь больше не проверяется:
    единственное место решения — gate_main(), see докстринг модуля."""
    repo = os.environ.get("GITHUB_REPOSITORY", "mytab0r/edge-harness")
    now = datetime.now(timezone.utc)
    token = os.environ.get("CLOUDFLARE_API_TOKEN")
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID")

    if not token or not account_id:
        print("quota_watch: CLOUDFLARE_API_TOKEN/CLOUDFLARE_ACCOUNT_ID не заданы — "
              "наблюдение невозможно в этом прогоне")
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


def main(argv: list[str] | None = None) -> int:
    """CLI-диспетчер: `gate` | `measure` — ДВА разных шага одного job'а
    (quota-watch.yml), см. докстринг модуля. Явные подкоманды, а не дефолт
    на «измерять» — ошибка вызова обязана быть громкой, а не тихо пропускать
    гейт троттлинга/простоя."""
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["gate"]:
        return gate_main()
    if argv == ["measure"]:
        return measure_main()
    print(f"::error::quota_watch: использование: quota_watch.py gate|measure (получено {argv!r})",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
