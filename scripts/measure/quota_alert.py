#!/usr/bin/env python3
"""Дедуп-эскалация + автозаведение задачи на переходе через порог квоты (#605).

Повод: 2026-09-06 суточная квота DO rows_read была превышена в полтора раза
(7 487 640 при лимите 5 000 000), находка обнаружена ЧЕЛОВЕКОМ, случайно
запустившим `quotas.yml` (workflow_dispatch, без расписания) спустя часы
после отказа. Мониторинг существовал (`scripts/measure/quotas.py::
over_threshold` + `pulse_guard.escalate`), но только по требованию —
превращать метрику в непрерывное наблюдение мало, если оно не переводит
находку в действие: владелец дословно — «в чём прикол мониторинга ошибок
который нихуя не исправляет».

Этот модуль — общее место правды «порог пробит/восстановлен → что делать»
для ДВУХ вызывающих с разной ценой прогона (`scripts/measure/quota_watch.py`:
дешёвая частая проверка ОДНОЙ метрики rows_read, и её же `full_sweep` — редкий
полный срез ВСЕХ метрик quotas.py) — один канал решения на класс «метрика
квоты пробила порог», а не два независимых, которые светили бы дублирующими
алертами на один и тот же ресурс просто потому, что его увидели оба сборщика.

Пять предохранителей:

  1. Дедупликация по ПЕРЕХОДУ состояния (issue #120: pulse_guard.WATCHDOG_ISSUE,
     тот же канал, что у предохранителя конвейера и пульса — второй не
     заводим). Маркер `[quota: состояние <ключ> = ok|breach ...]` хранит
     ПОСЛЕДНЕЕ известное состояние КОНКРЕТНОГО ресурса (ключ — тот же, что
     `quotas.py.LIMITS`, например `cf_do_rows_read_day`) — сигнал уходит
     только когда состояние ИЗМЕНИЛОСЬ (ok→breach или breach→ok), не на
     каждый прогон. Пробитый порог, который держится час за часом, не
     плодит новый алерт каждые 15 минут.
  2. Действие, не констатация: на переходе ok→breach модуль сам заводит (или,
     если гвардия дублей `scripts/lib/duplicate_guard.py` находит уже
     открытую задачу того же класса, — комментирует найденную) задачу через
     `scripts/gh/issue-create` — единственный документированный путь
     заведения issue (#526), с меткой `area:process` (уровень 1 приоритета
     выбора свободной задачи, `scripts/lib/free_task.py::issue_priority_key`,
     #361) — задача разбора расхода не тонет в общем пуле, берётся раньше
     остальных. Номер заведённой/найденной задачи попадает в маркер
     состояния и в текст алерта — Telegram-сообщение и след в #120 несут
     конкретный номер, не только факт.
  3. Не закрывается автоматически по возврату метрики ниже порога: суточный
     счётчик Cloudflare сбрасывается в 00:00 UTC сам по себе — это не
     означает, что причина расхода понята и устранена. Переход breach→ok
     оставляет комментарий-уведомление (Telegram + след в #120), задача
     остаётся открытой до разбора человеком/агентом.
  4. Два предохранителя от «дедуп по переходу» с ложным переходом (найдено
     ревью PR #607, живой прогон): первое наблюдение ресурса, заставшее его
     уже в норме (prev_state is None, new_state == "ok"), — НЕ переход
     breach→ok, эскалации не было бы о чём сообщать, поэтому наружу тихо
     уходит только маркер состояния, без Telegram/следа в #120. А если на
     переходе ok→breach `create_or_note_task` не смог завести/найти задачу
     (issue_number is None — сеть, отказ гвардии дублей без распознанного
     кандидата, любая иная ошибка issue-create) — маркер breach НЕ пишется:
     иначе следующий прогон увидел бы «без изменений» и промолчал бы по
     дедупу до случайного breach→ok (для rows_read — сутки, для
     storage-метрики — пока сама не упадёт), а действие («заводит задачу»,
     суть #605) так и не повторилось бы.
  5. Отказ носителя дедупа различим вызывающим, а не только строкой лога
     (found: ревью PR #607, head 345a64f): тихая запись маркера (первое
     наблюдение в норме) возвращает вердикт `pulse_guard.carrier_write_
     verdict` — той же лексикой, что `escalate`, — и вызывающий
     (`quota_watch.measure_main::_delivery_exit_failed`) краснит его
     предикатом `pulse_guard.escalation_dedup_carrier_failed`; breach-переход
     проходит через сам `escalate`, и его вердикт краснится теми же
     предикатами. Иначе носитель дедупа мог стоять сломанным неограниченно
     долго при зелёных прогонах: каждый тик повторял бы страницу/попытку
     действия, и ни один прогон не говорил бы об этом.

## Тренд и третье состояние (#1100)

Живой инцидент 2026-09-13: quota-watch увидел `gh_rest_rate_limit_hour` в
07:03 (6.1%, "ok") и промолчал 83 минуты подряд до фактического исчерпания
токена прогонов — состояние всё это время оставалось "ok", порог (80%)
пробился только в последние минуты. Дедуп по переходу выше видит только
ДИСКРЕТНОЕ ok/breach одной метрики за раз — он структурно не может
предупредить о РОСТЕ, если рост не успел пересечь порог. Разбор нашёл и
второй, независимый дефект того же прогона: `all_issue_comments(max_pages=1)`
читал ПЕРВУЮ страницу комментариев #120 как «самую свежую» — но у эндпоинта
GitHub «List issue comments» нет `sort`/`direction`, страница 1 — это САМАЯ
СТАРАЯ сотня комментариев растущей истории (#120 — 950+ на момент разбора).
`last_state` поэтому ВСЕГДА видел «маркера нет» и объявлял каждый тик
«первым наблюдением» — 114 копий одной и той же записи в #120 вместо одного
перехода (см. `pulse_guard.all_issue_comments`, фикс и разбор там).

Третье состояние — `STATE_APPROACHING` (`classify_state`, `TREND_HORIZON_
MINUTES`): по ДВУМ последовательным числовым показаниям (`last_reading`/
`record_reading`, см. ниже) вычисляется скорость роста и проекция «через
сколько минут при этой скорости будет пробит порог»; если проекция короче
горизонта — сигнал уходит ДО того, как pct сам пересечёт порог. Показание
пишется КАЖДЫЙ тик независимо от состояния (в отличие от `state_marker`,
который пишется только на переходе) — но НЕ новым комментарием: носитель
редактируется на месте (`pulse_guard.edit_issue_comment`), поэтому счётчик
комментариев #120 не растёт с частотой тиков (тот же класс, что и сам
инцидент — 114 дублей).

Запуск тестов: python -m pytest scripts/measure/test_quota_alert.py -q
"""

from __future__ import annotations

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent.parent / "lib" / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import importlib.util
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Канал эскалации/маркеров — issue #120, тот же, что у предохранителя
# конвейера (см. докстринг pulse_guard.py) — второй канал не заводим.
_PG_PATH = Path(__file__).resolve().parents[1] / "orchestra" / "pulse_guard.py"
_pg_spec = importlib.util.spec_from_file_location("pulse_guard", _PG_PATH)
pulse_guard = importlib.util.module_from_spec(_pg_spec)
_pg_spec.loader.exec_module(pulse_guard)  # type: ignore[union-attr]

WATCHDOG_ISSUE = pulse_guard.WATCHDOG_ISSUE

# Порог по умолчанию — ОДНО место правды (quotas.THRESHOLD_PCT), не второй
# независимый литерал 80.0 (found: ревью PR #607, некритичное замечание):
# оба вызывающих (quota_watch.py::cheap_check/full_sweep) уже передают
# threshold=quotas.THRESHOLD_PCT явно, но дефолт этой сигнатуры (для прямых
# вызовов/тестов) обязан отслеживать то же число, иначе правка одного не
# долетела бы до другого молча.
_QZ_PATH = Path(__file__).with_name("quotas.py")
_qz_spec = importlib.util.spec_from_file_location("quotas", _QZ_PATH)
quotas = importlib.util.module_from_spec(_qz_spec)
_qz_spec.loader.exec_module(quotas)  # type: ignore[union-attr]

REPO_ROOT = Path(__file__).resolve().parents[2]
ISSUE_CREATE = REPO_ROOT / "scripts" / "gh" / "issue-create"

# Метки заведённой задачи — Python-константы `*_LABEL` собираются в реестр
# scripts/lib/collect_labels.py (Форма 1), новую строку в docs/agents/LABELS.md
# требует только QUOTA_BREACH_LABEL — остальные три уже описаны там другими
# сеятелями.
TASK_LABEL = "task"
AREA_PROCESS_LABEL = "area:process"  # уровень 1 приоритета выбора (#361)
AUTO_LABEL = "auto-detected"
QUOTA_BREACH_LABEL = "quota-breach"

def _fmt(n: float) -> str:
    """Число с пробелом-разделителем разрядов — на уже готовой строке
    ПУНКТУАЦИОННЫЕ запятые (перечисление, «текущее/лимит, %») не трогает:
    раньше `.replace(",", " ")` на всём собранном тексте схлопывал в пробел
    и разделитель разрядов, и соседнюю запятую-пунктуацию — живой пример
    "120 000 / 5 000 000  2.4%" (двойной пробел вместо ", ") — найдено
    мутационной проверкой этого модуля."""
    return f"{n:,.0f}".replace(",", " ")


STATE_MARKER_PREFIX = "[quota: состояние"

# Три состояния, не два (#1100, тот же класс, что #1096): "ok" — норма;
# "approaching" — тренд показывает, что порог THRESHOLD_PCT будет пробит
# раньше TREND_HORIZON_MINUTES минут (см. classify_state ниже); "breach" —
# порог уже пробит (как раньше). "approaching" — предупреждение ДО отказа:
# инцидент #1100 показал, что сторож видел 6.1% в норме и молчал все 83
# минуты до фактического исчерпания лимита — состояние держалось "ok" всю
# дорогу, порог пробился только в самом конце. Без промежуточного состояния,
# завязанного на СКОРОСТЬ роста, а не только на текущее значение, сигнал
# физически не мог прийти раньше самого порога.
STATE_OK = "ok"
STATE_APPROACHING = "approaching"
STATE_BREACH = "breach"


def _state_marker_prefix(resource_key: str) -> str:
    return f"{STATE_MARKER_PREFIX} {resource_key} = "


_STATE_RE = re.compile(r"= (breach|approaching|ok)(?: issue=#(\d+))?\]")

# Строка кандидата гвардии дублей в stderr issue-create — буквально
# `  #<num> (score <s>): <title> — <url>` (scripts/gh/issue-create, awk-строка
# после дедупликации #566); голый "#(\d+)" ловил бы и номер класса дефекта
# в описании ошибки ("класс #566, живой случай #518/…") ВМЕСТО номера
# кандидата (found: ревью PR #607).
_CANDIDATE_LINE_RE = re.compile(r"#(\d+) \(score [0-9.]+\): (.+?) — (?:https?://\S+)\s*$",
                                re.MULTILINE)


# Сколько СВЕЖИХ страниц комментариев #120 читать для дедупа состояния
# ресурса — одно место правды для всей семьи сторожа квот (quota_watch.py
# берёт тем же именем для своих stale-маркеров). Обоснование и честная цена
# деградации — в докстринге pulse_guard.all_issue_comments и у константы
# в quota_watch.py (found: ревью PR #607 — некритичное замечание «last_state
# читает #120 без ограничения страниц» = тот же «хвост», что уже закрыт для
# stale-маркеров простоя; замер 2026-09-10: в #120 больше 550 комментариев).
MARKER_SCAN_PAGES = 1


def last_state(repo: str, resource_key: str) -> tuple[str | None, int | None]:
    """Последнее записанное состояние КОНКРЕТНОГО ресурса и номер связанной
    задачи (если он был в маркере) — по самому свежему из подходящих
    комментариев #120 СВЕЖЕЙ СТРАНИЦЫ (MARKER_SCAN_PAGES — реально
    вычисленной ПОСЛЕДНЕЙ страницы, см. `pulse_guard.all_issue_comments`,
    фикс #1100: страница 1 у этого эндпоинта всегда самая СТАРАЯ, не
    свежая). Самый свежий маркер ключа лежит на свежей странице, пока после
    него не накопилось 100 более новых комментариев; деградация — тот же
    самозаживающийся повторный сигнал раз в ~сутки затяжного эпизода, не
    тишина (первое наблюдение breach тоже алертит). Нет ни одного маркера
    на странице — состояние не известно (None, None)."""
    prefix = _state_marker_prefix(resource_key)
    matches = pulse_guard.issue_markers_any(repo, WATCHDOG_ISSUE, (prefix,),
                                            max_pages=MARKER_SCAN_PAGES)
    if not matches:
        return None, None
    _, body = max(matches, key=lambda pair: pair[0])
    idx = body.find(prefix)
    m = _STATE_RE.search(body, idx)
    if not m:
        return None, None
    state = m.group(1)
    issue_number = int(m.group(2)) if m.group(2) else None
    return state, issue_number


def state_marker(resource_key: str, state: str, issue_number: int | None) -> str:
    suffix = f" issue=#{issue_number}" if issue_number else ""
    return f"{_state_marker_prefix(resource_key)}{state}{suffix}]"


# ── Тренд (#1100): числовое показание последнего тика, не только ok/breach ──
#
# state_marker выше несёт ТОЛЬКО дискретное состояние и пишется только на
# ПЕРЕХОДЕ (дедуп, см. докстринг модуля) — «6.1%, без изменений» никогда не
# попадает в #120 вообще. Для тренда нужна числовая история: предыдущее
# показание (pct, когда) НЕЗАВИСИМО от того, поменялось ли состояние. Если
# копить эту историю обычными POST-комментариями на каждый тик — это ровно
# тот же класс, что породил инцидент (114 копий одного маркера): комментарий
# читается редактированием ОДНОГО и того же комментария (edit_issue_comment)
# — счётчик комментариев issue не растёт с частотой тиков, а данные для
# тренда переживают между тиками.
READING_MARKER_PREFIX = "[quota: замер"


def _reading_marker_prefix(resource_key: str) -> str:
    return f"{READING_MARKER_PREFIX} {resource_key} = "


_READING_RE = re.compile(r"= ([0-9.]+)% at (\S+)\]")


def reading_marker(resource_key: str, pct: float, when: datetime) -> str:
    return f"{_reading_marker_prefix(resource_key)}{pct}% at {when.isoformat()}]"


def last_reading(repo: str, resource_key: str) -> tuple[float, datetime, int] | None:
    """Последнее числовое показание ресурса (pct, момент, id комментария-
    носителя) — по самой свежей СТРАНИЦЕ #120 (MARKER_SCAN_PAGES, тот же
    приём и та же цена деградации, что last_state выше). None — показаний
    ещё не было (первый тик этого ресурса вообще, либо запись показания
    раньше не удавалась — см. record_reading)."""
    prefix = _reading_marker_prefix(resource_key)
    comments = pulse_guard.all_issue_comments(repo, WATCHDOG_ISSUE, max_pages=MARKER_SCAN_PAGES)
    candidates = [c for c in comments if prefix in (c.get("body") or "")]
    if not candidates:
        return None
    newest = max(candidates, key=lambda c: pulse_guard.parse_time(c["created_at"]))
    body = newest.get("body") or ""
    idx = body.find(prefix)
    m = _READING_RE.search(body, idx)
    if not m:
        return None
    try:
        when = pulse_guard.parse_time(m.group(2))
    except ValueError:
        return None
    return float(m.group(1)), when, newest["id"]


def record_reading(repo: str, resource_key: str, pct: float, when: datetime,
                    comment_id: int | None) -> None:
    """Правит существующий носитель (edit_issue_comment) — заводит новый
    ТОЛЬКО если носителя ещё не было (comment_id is None, первый тик этого
    ресурса). Best-effort по построению (см. check_and_alert): отказ здесь не
    должен красить прогон (тренд — предупреждение раньше отказа, не сам
    канал эскалации порога/простоя, у которых уже есть свой fail loud), но
    обязан быть ВИДИМ (::warning::), а не тихим — иначе носитель тренда мог
    бы стоять сломанным неограниченно долго при зелёных прогонах, тем же
    классом, что дедуп состояния уже проходил (см. carrier_write_verdict).

    Честная деградация (found: ревью PR #1112): PATCH меняет только
    `updated_at` комментария, GitHub листает `.../comments` по ПОРЯДКУ
    СОЗДАНИЯ (`created_at`) — редактирование на месте НИКОГДА не двигает
    позицию носителя в этом порядке. Значит по мере роста #120 носитель,
    созданный один раз и вечно редактируемый, рано или поздно физически
    съезжает за окно `MARKER_SCAN_PAGES`, сколько бы раз его ни правили —
    тот же класс амнезии, что и сам инцидент #1100, только для числового
    носителя тренда, а не для маркера состояния. Самоисцеление: тик, не
    нашедший носитель (`last_reading` вернёт None, хотя он существует и
    просто уехал за страницу), теряет ровно один сэмпл тренда в ЭТОМ тике
    (classify_state падает на чистый pct>=threshold — та же семантика, что
    у прежнего двухсостоячного дедупа), но `record_reading` получает
    comment_id=None и заводит НОВЫЙ носитель у хвоста истории — на
    следующем тике позиция снова свежая. Проверено
    test_reading_carrier_falling_off_fresh_page_self_heals_next_tick."""
    text = reading_marker(resource_key, pct, when)
    if comment_id is not None:
        pulse_guard.edit_issue_comment(repo, comment_id, text)
    else:
        pulse_guard.post_issue_comment(repo, WATCHDOG_ISSUE, text)


# Горизонт «приближения к пределу» — раньше был получен «тем же приёмом
# кратности», что MEASUREMENT_STALE_MINUTES (quota_watch.py) и
# HEARTBEAT_MAX_AGE_MINUTES (pulse_guard.py): 3×CHECK_INTERVAL_MINUTES=45.
# Ревизия #1184 разделила эти две константы: MEASUREMENT_STALE_MINUTES
# отвечает на вопрос «сколько может молчать СОБСТВЕННЫЙ триггер quota-watch.
# yml, прежде чем считать замер простаивающим» (обоснование — реальная,
# измеренная каденция этого триггера, см. quota_watch.py) и теперь поднята
# до 90 минут по этой причине. TREND_HORIZON_MINUTES отвечает на ДРУГОЙ
# вопрос — «на сколько минут вперёд экстраполировать ТЕКУЩИЙ темп роста
# метрики, чтобы предупредить ДО порога» — и его число обязано идти от
# риск-профиля метрики (скорость исчерпания), а не от каденции чужого
# триггера: то, как часто вообще случится тик, не определяет, сколько
# времени в будущее безопасно экстраполировать линейный тренд. Совпадение
# числа с MEASUREMENT_STALE_MINUTES было случайным следствием общего
# приёма, не содержательной связью — оставлять их искусственно равными
# после того, как первое пересчитано по НЕсвязанной причине, значило бы
# тащить чужой такт в эту формулу без обоснования. Число 45 здесь сохранено
# (не унаследовано от связи с MEASUREMENT_STALE_MINUTES, а проверено
# независимо): инцидент #1100 (полный путь 6.1%→100% занял 83 минуты) — два
# теста в test_quota_alert.py реконструируют его на реальной скорости роста:
# test_reproduction_1100_trend_fires_before_exhaustion (тик
# CHECK_INTERVAL_MINUTES=15, ~53 минуты запаса до исчерпания и ~46 минут до
# первого отказа оркестратора) и test_trend_horizon_catches_1100_incident_
# before_exhaustion (находка ai-review PR #1185: тик 1 минута — сосед выше
# бакетируется 15-минутными точками и не ловит дрейф TREND_HORIZON_MINUTES
# внутри одного тика, этот тест точнее и читает константу напрямую) — оба
# подтверждение достаточности 45 минут, не копия чужого числа. Единственное
# определение (не дублируется в другом модуле): прежняя гвардия дрейфа
# сверяла это число с ЧУЖОЙ формулой (CHECK_INTERVAL_MINUTES × 3 в
# quota_watch.py), а не с копией самого себя — ревизия #1184 сняла и
# гвардию, и формулу как проверяющую не то (см. git-историю
# test_quota_watch.py).
TREND_HORIZON_MINUTES = 45.0


def _trend_projection(prev_pct: float, prev_time: datetime, current_pct: float,
                       now: datetime, threshold: float) -> tuple[float | None, float | None]:
    """(скорость %/мин, минут до порога) по двум последовательным показаниям.
    Скорость None — время между показаниями не положительное (повтор тика/
    рассинхрон часов) — тренд посчитать не из чего. Минуты до порога None —
    порог уже пробит (не это вычисляет approaching, этим занимается
    classify_state через сам pct) либо скорость не растёт (<=0, порог не
    приближается ростом; уменьшение — не тревога)."""
    delta_minutes = (now - prev_time).total_seconds() / 60.0
    if delta_minutes <= 0:
        return None, None
    rate = (current_pct - prev_pct) / delta_minutes
    if rate <= 0 or current_pct >= threshold:
        return rate, None
    return rate, (threshold - current_pct) / rate


def classify_state(pct: float, threshold: float, projected_minutes: float | None,
                    horizon: float) -> str:
    if pct >= threshold:
        return STATE_BREACH
    if projected_minutes is not None and projected_minutes <= horizon:
        return STATE_APPROACHING
    return STATE_OK


def _same_resource_candidates(stderr: str, resource_label: str) -> list[int]:
    """Номера кандидатов гвардии дублей, в чьих ЗАГОЛОВКАХ упомянут этот же
    ресурс (found: ревью PR #607, некритичное замечание «гвардия дублей
    смешивает ресурсы квот»): шаблонные заголовки «Квота харнеса перевалила
    за 80.0%: <ресурс>» дают jaccard 0.38–0.50 между РАЗНЫМИ ресурсами при
    пороге 0.3 — без этой проверки улика по пробитию одного ресурса уходила
    бы комментарием в чужую задачу, а своя не заводилась вовсе (в день
    инцидента #324 повышены были сразу несколько CF-метрик, сценарий
    типовой)."""
    numbers = []
    for num, title in _CANDIDATE_LINE_RE.findall(stderr):
        if resource_label in title:
            numbers.append(int(num))
    return numbers


def create_or_note_task(repo: str, resource_label: str, resource_key: str,
                         current: float, limit: float, pct: float, threshold: float) -> tuple[int | None, str]:
    """Заводит задачу через scripts/gh/issue-create (гвардия `task`-метки +
    гвардия похожести заголовка #566). Если гвардия дублей нашла уже
    открытую задачу — задача НЕ дублируется, найденная получает комментарий
    с новой уликой (см. докстринг модуля, «действие, не констатация»);
    «найденной» считается только задача ТОГО ЖЕ ресурса (метка ресурса в
    заголовке кандидата), похожие задачи других ресурсов не глотают улику —
    заводим свою с --confirm-not-duplicate (см. _same_resource_candidates).
    Возвращает (номер задачи или None, диагностика)."""
    title = f"Квота харнеса перевалила за {threshold}%: {resource_label}"
    now = datetime.now(timezone.utc).isoformat()
    body = (
        f"Автоматическое обнаружение (`scripts/measure/quota_alert.py`): метрика "
        f"«{resource_label}» перевалила за порог {threshold}%.\n\n"
        f"- Текущее значение: {_fmt(current)} / {_fmt(limit)} ({pct}%)\n"
        + f"- Обнаружено: {now}\n"
        + "- Полный срез всех квот харнеса (Cloudflare + GitHub в одной таблице): "
          "`gh workflow run quotas.yml --repo mytab0r/edge-harness` (scripts/measure/quotas.py)\n\n"
        "Что дальше:\n"
        "1. Снять полный срез и понять, кто/что расходует ресурс сверх обычного профиля.\n"
        "2. Найти и снизить источник расхода (см. docs/research/20-cloudflare-free.md, "
        "«Замер факта: rows_read в проде» — известный горячий путь, если расход снова там).\n"
        "3. Если расход органический (рост трафика, не дефект) — рассмотреть смену "
        "тарифа/лимита, а не только оптимизацию кода.\n\n"
        "Задача закрывается, когда расход объяснён и (если это был дефект) исправлен — "
        "НЕ автоматически по возврату метрики ниже порога: суточный счётчик Cloudflare "
        "сбрасывается в 00:00 UTC сам по себе, это не равно «причина устранена».\n\n"
        # #720: тело обязано нести машиночитаемое объявление связи, иначе
        # scripts/gh/issue-create откажет ДО сетевого вызова. Алерт квоты
        # зависимости не знает — явное «ничем», не пропуск.
        "БЛОКИРУЕТСЯ: ничем"
    )
    args = [str(ISSUE_CREATE), "--title", title, "--body", body,
            "--label", TASK_LABEL, "--label", AREA_PROCESS_LABEL,
            "--label", AUTO_LABEL, "--label", QUOTA_BREACH_LABEL]
    result = subprocess.run(
        args,
        capture_output=True, text=True, encoding="utf-8",
    )
    if result.returncode == 0:
        m = re.search(r"/issues/(\d+)\s*$", result.stdout.strip())
        issue_number = int(m.group(1)) if m else None
        return issue_number, (f"задача заведена ({result.stdout.strip()})" if issue_number else
                               f"issue-create завершился успешно, но номер не распознан из вывода: {result.stdout.strip()!r}")
    # Гвардия дублей (#566) отказывает ИМЕННО так, когда похожая ОТКРЫТАЯ
    # задача уже есть. Похожая ≠ та же: улика уходит в найденную задачу
    # только если это задача ПРО ЭТОТ ЖЕ ресурс (метка ресурса в заголовке);
    # похожие задачи других ресурсов — не получатель чужой улики, своя
    # задача заводится с --confirm-not-duplicate (found: ревью PR #607 —
    # jaccard шаблонных заголовков разных ресурсов 0.38–0.50 при пороге 0.3).
    if "похожие ОТКРЫТЫЕ задачи пула уже есть" in result.stderr:
        same_resource = _same_resource_candidates(result.stderr, resource_label)
        if same_resource:
            existing = same_resource[0]
            note = (f"Новая улика (без второй задачи — гвардия дублей #566 нашла эту "
                     f"открытой, тот же ресурс «{resource_label}»): «{resource_label}» снова "
                     f"{_fmt(current)}/{_fmt(limit)} ({pct}%), {now}")
            try:
                pulse_guard.post_issue_comment(repo, existing, note)
            except RuntimeError as error:
                # Формулировка НАРОЧНО не содержит литералов вердикта
                # escalate («НЕ оставлен»): маркер состояния здесь УЖЕ
                # записан escalate'ом (носитель дедупа цел, повторной
                # страницы не будет), и предикат
                # pulse_guard.escalation_dedup_carrier_failed не должен
                # принимать потерянную улику за сломанный носитель — иначе
                # measure_main краснил бы прогон ложным разбором (found:
                # ревью PR #607, head 345a64f — source-гвардия запрещает
                # вторые копии литералов вне pulse_guard).
                # Аннотация ::warning:: — носитель видимости вне сырого лога
                # (чеклист ревью PR #607, head 345a64f: «потерянная улика
                # видна только в логе прогона»): улика добавляется ОДИН раз
                # на переход, маркер уже записан — повторной попытки не
                # будет, потеря не должна пройти невидимой; аннотацию GitHub
                # показывает в списке аннотаций проверки, не только в логе.
                print(f"::warning::quota_alert: улика о пробитии порога «{resource_label}» "
                      f"не добавлена в открытую задачу #{existing}: {error}", file=sys.stderr)
                return existing, f"задача #{existing} уже открыта, комментарий с уликой не добавлен: {error}"
            return existing, f"задача #{existing} уже открыта — добавлена новая улика"
        confirm = (f"другой ресурс квоты: порог пробил «{resource_label}», а в заголовках похожих "
                    "открытых задач этот ресурс не упомянут — улика другого ресурса не может "
                    "заменить задачу на разбор этого (ревью PR #607)")
        retry = subprocess.run(
            args + ["--confirm-not-duplicate", confirm],
            capture_output=True, text=True, encoding="utf-8",
        )
        if retry.returncode == 0:
            m = re.search(r"/issues/(\d+)\s*$", retry.stdout.strip())
            if m:
                return int(m.group(1)), (f"задача заведена с --confirm-not-duplicate "
                                          f"(похожие задачи — другие ресурсы квоты): {retry.stdout.strip()}")
            return None, (f"issue-create завершился успешно с --confirm-not-duplicate, но номер не "
                           f"распознан из вывода: {retry.stdout.strip()!r}")
        return None, f"issue-create отказал повторно (с --confirm-not-duplicate): {retry.stderr.strip()[:400]}"
    return None, f"issue-create отказал: {result.stderr.strip()[:400]}"


_READING_NOT_GIVEN = object()  # сентинел «вызывающий не передал» ≠ None


def check_and_alert(repo: str, resource_key: str, resource_label: str,
                     current: float, limit: float, pct: float,
                     threshold: float = quotas.THRESHOLD_PCT, *,
                     trend_horizon_minutes: float = TREND_HORIZON_MINUTES,
                     now: datetime | None = None,
                     prev_reading: object = _READING_NOT_GIVEN) -> str:
    """Единая точка входа обоих вызывающих (см. докстринг модуля). Возвращает
    строку для лога прогона — вызывающий печатает её и решает про exit code
    предикатами pulse_guard (escalation_channel_failed /
    escalation_dedup_carrier_failed) через quota_watch._delivery_exit_failed;
    отказ тихой записи маркера (первое наблюдение в норме) приходит вердиктом
    carrier_write_verdict и ловится тем же вторым предикатом.

    Три состояния, не два (#1100): ok / approaching (тренд, см. classify_state
    и TREND_HORIZON_MINUTES) / breach. Показание (pct, `now`) пишется в
    носитель тренда (record_reading) НА КАЖДОМ вызове, независимо от того,
    изменилось состояние или нет — тренду следующего тика нужна числовая
    история, а не только дискретный ok/breach, который дедуп прежде писал
    только на переходах (инцидент #1100: сторож видел 6.1%, молчал 83
    минуты, порог пробился в самом конце — состояние всё это время было
    "ok", числовой историей для расчёта скорости роста никто не был).

    `prev_reading` (found: ревью PR #1112, замер «4 из 14 REST-вызовов на
    замер-тик — чистый дубль») — вызывающий, которому УЖЕ известно последнее
    показание (`quota_watch.github_rate_limit_main` читает его сам для
    СВОЕГО троттлинга ДО вызова этой функции), может передать его явно —
    внутренний повторный `last_reading` не звонит сети во второй раз за тот
    же самый факт. Сентинел `_READING_NOT_GIVEN`, не `None`, — вызывающий,
    которому НЕЧЕГО передать (уже знает, что показаний не было), обязан
    мочь сказать это явно (`prev_reading=None`) без риска, что такой вызов
    молча включит собственный (лишний) сетевой поход за тем же фактом.

    Первое наблюдение ресурса (prev_state is None), заставшее его уже в
    норме, — не переход breach→ok (события восстановления не было, эскалация
    сообщила бы о факте, которого не было), поэтому наружу тихо уходит только
    маркер состояния, без Telegram/следа-эскалации в #120 (найдено ревью
    PR #607 — до этой правки такой прогон слал ложное «квота вернулась ниже
    порога» на КАЖДЫЙ ресурс, впервые увиденный в норме). Первое наблюдение,
    заставшее ресурс уже в approaching/breach, — НАСТОЯЩАЯ новость (симметрично
    уже существовавшему поведению для breach) и эскалирует как обычно."""
    now = now or datetime.now(timezone.utc)

    if prev_reading is _READING_NOT_GIVEN:
        prev_reading = last_reading(repo, resource_key)
    rate = projected_minutes = None
    if prev_reading is not None:
        prev_pct, prev_time, _ = prev_reading
        rate, projected_minutes = _trend_projection(prev_pct, prev_time, pct, now, threshold)
    new_state = classify_state(pct, threshold, projected_minutes, trend_horizon_minutes)

    prev_state, prev_issue = last_state(repo, resource_key)

    # Показание для тренда — ВСЕГДА, best-effort, тихо (это НЕ канал сигнала,
    # см. докстринг record_reading): отказ здесь не должен красить прогон —
    # у канала эскалации порога/простоя уже есть свой fail loud ниже — но
    # обязан быть виден.
    try:
        record_reading(repo, resource_key, pct, now, prev_reading[2] if prev_reading else None)
    except RuntimeError as error:
        print(f"::warning::quota_alert: показание «{resource_label}» ({pct}%) не записано для "
              f"тренда: {error} — на следующем тике тренд посчитать не из чего", file=sys.stderr)

    if prev_state is None and new_state == STATE_OK:
        # Носитель дедупа пишется напрямую (post_issue_comment, без
        # эскалации — см. предохранитель 4 в докстринге модуля), поэтому
        # отказ здесь не проходит через вердикт escalate и обязан быть
        # различим вызывающему отдельно (found: ревью PR #607, head 345a64f
        # — раньше отказ возвращался строкой «маркер НЕ записан», которую
        # вызывающий не прогонял ни через один предикат: носитель дедупа
        # мог стоять сломанным неограниченно долго при зелёных прогонах).
        # Вердикт строит pulse_guard.carrier_write_verdict — той же лексикой,
        # что escalate, литералы рождаются там же, рядом с предикатами; строку
        # отказа ловит pulse_guard.escalation_dedup_carrier_failed, и
        # вызывающий (quota_watch.measure_main:: _delivery_exit_failed)
        # краснит по ней прогон.
        try:
            pulse_guard.post_issue_comment(repo, WATCHDOG_ISSUE, state_marker(resource_key, STATE_OK, None))
            verdict = pulse_guard.carrier_write_verdict(WATCHDOG_ISSUE, True)
        except RuntimeError as error:
            verdict = pulse_guard.carrier_write_verdict(WATCHDOG_ISSUE, False, str(error))
        return f"{resource_key}: первое наблюдение (ok, {pct}%) — {verdict}"

    if prev_state == new_state:
        return f"{resource_key}: без изменений ({new_state}, {pct}%) — сигнал не отправлен (дедуп)"

    if new_state == STATE_BREACH:
        issue_number, note = create_or_note_task(repo, resource_label, resource_key,
                                                   current, limit, pct, threshold)
        action = f"задача: #{issue_number}" if issue_number else f"задача НЕ заведена ({note})"
        text = (
            f"🚨 edge-harness: квота «{resource_label}» перевалила за {threshold}%: "
            f"{_fmt(current)} / {_fmt(limit)} ({pct}%). {action}."
        )
        if issue_number is None:
            # Действие (заведение задачи) не состоялось — маркер состояния
            # НЕ пишем: следующий прогон обязан снова увидеть переход
            # (prev_state ≠ "breach") и повторить попытку, а не замолчать
            # по дедупу до случайного breach→ok (found: ревью PR #607).
            result = pulse_guard.escalate(repo, WATCHDOG_ISSUE, text)
            return (f"{resource_key}: breach — {result}; {note}; маркер состояния НЕ записан "
                     "(действие не состоялось) — следующий прогон повторит попытку")
        text += "\n" + state_marker(resource_key, STATE_BREACH, issue_number)
        result = pulse_guard.escalate(repo, WATCHDOG_ISSUE, text)
        return f"{resource_key}: breach — {result}; {note}"

    if new_state == STATE_APPROACHING:
        # Approaching — предупреждение раньше отказа (#1100), не задача:
        # заводить issue уже на этом рубеже дублировало бы дедуп-заголовок
        # breach (create_or_note_task матчит кандидатов по resource_label в
        # заголовке) и создавало бы задачу на тренд, который может развернуться
        # сам, не дойдя до порога. Telegram + след в #120 — та же видимость,
        # что и у breach, без автозаведения.
        if prev_reading is not None:
            ago_minutes = (now - prev_reading[1]).total_seconds() / 60.0
            trend_note = (f"тренд: было {prev_reading[0]}% {ago_minutes:.0f} мин назад, сейчас "
                          f"{pct}% — при скорости {rate:.2f} п.п./мин порог {threshold}% будет "
                          f"пробит через {projected_minutes:.0f} мин, если рост не остановится.")
        else:
            trend_note = "тренд ещё не накоплен (первое показание уже в зоне приближения)."
        text = (
            f"⚠️ edge-harness: квота «{resource_label}» приближается к пределу {threshold}%: "
            f"{_fmt(current)} / {_fmt(limit)} ({pct}%). {trend_note}\n"
            + state_marker(resource_key, STATE_APPROACHING, prev_issue)
        )
        result = pulse_guard.escalate(repo, WATCHDOG_ISSUE, text)
        return f"{resource_key}: approaching — {result}"

    # new_state == STATE_OK и prev_state не None — настоящий возврат в норму,
    # не первое наблюдение. Текст различает, ОТКУДА вышли (found: ревью PR
    # #1112): из breach — порог реально был пробит, текст так и говорит
    # («вернулась ниже») и напоминает про задачу на разбор; из approaching —
    # порог НИКОГДА не пробивался (это был только прогноз тренда), текст
    # «вернулась ниже» был бы ложным восстановлением НЕСУЩЕСТВОВАВШЕГО
    # инцидента для читателя #120, пропустившего ⚠️-запись — разворот тренда
    # говорит явно, что порог не был достигнут.
    if prev_state == STATE_BREACH:
        text = (
            f"✅ edge-harness: квота «{resource_label}» вернулась ниже {threshold}% "
            f"({_fmt(current)} / {_fmt(limit)}, {pct}%). "
            + (f"Задача на разбор по маркеру: #{prev_issue} (текущее её состояние здесь "
               "не проверялось)." if prev_issue
               else "Прежней задачи на разбор в маркере не найдено.")
        )
    else:
        text = (
            f"✅ edge-harness: квота «{resource_label}» — рост остановился, порог {threshold}% "
            f"НЕ был достигнут ({_fmt(current)} / {_fmt(limit)}, {pct}%). Предупреждение "
            "«приближается к пределу» снято, задачи на разбор не было (approaching не заводит "
            "задачу, см. докстринг check_and_alert)."
        )
    text += "\n" + state_marker(resource_key, STATE_OK, prev_issue)
    result = pulse_guard.escalate(repo, WATCHDOG_ISSUE, text)
    return f"{resource_key}: recovery — {result}"


if __name__ == "__main__":
    print(__doc__)
    sys.exit(0)
