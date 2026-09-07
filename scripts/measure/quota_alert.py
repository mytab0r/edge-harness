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

Три предохранителя:

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

Запуск тестов: python -m pytest scripts/measure/test_quota_alert.py -q
"""

from __future__ import annotations

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


def _state_marker_prefix(resource_key: str) -> str:
    return f"{STATE_MARKER_PREFIX} {resource_key} = "


_STATE_RE = re.compile(r"= (breach|ok)(?: issue=#(\d+))?\]")


def last_state(repo: str, resource_key: str) -> tuple[str | None, int | None]:
    """Последнее записанное состояние КОНКРЕТНОГО ресурса и номер связанной
    задачи (если он был в маркере) — по самому свежему из подходящих
    комментариев #120. Нет ни одного маркера — состояние не известно
    (None, None), первое наблюдение breach тоже должно алертить."""
    prefix = _state_marker_prefix(resource_key)
    matches = pulse_guard.issue_markers_any(repo, WATCHDOG_ISSUE, (prefix,))
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


def create_or_note_task(repo: str, resource_label: str, resource_key: str,
                         current: float, limit: float, pct: float, threshold: float) -> tuple[int | None, str]:
    """Заводит задачу через scripts/gh/issue-create (гвардия `task`-метки +
    гвардия похожести заголовка #566). Если гвардия дублей нашла уже
    открытую задачу — задача НЕ дублируется, найденная получает комментарий
    с новой уликой (см. докстринг модуля, «действие, не констатация»).
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
        "сбрасывается в 00:00 UTC сам по себе, это не равно «причина устранена»."
    )
    result = subprocess.run(
        [str(ISSUE_CREATE), "--title", title, "--body", body,
         "--label", TASK_LABEL, "--label", AREA_PROCESS_LABEL,
         "--label", AUTO_LABEL, "--label", QUOTA_BREACH_LABEL],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        m = re.search(r"/issues/(\d+)\s*$", result.stdout.strip())
        issue_number = int(m.group(1)) if m else None
        return issue_number, (f"задача заведена ({result.stdout.strip()})" if issue_number else
                               f"issue-create завершился успешно, но номер не распознан из вывода: {result.stdout.strip()!r}")
    # Гвардия дублей (#566) отказывает ИМЕННО так, когда похожая ОТКРЫТАЯ
    # задача уже есть — это не сбой, а сигнал «уже разбирается», найденный
    # номер получает комментарий вместо второй задачи того же класса.
    if "похожие ОТКРЫТЫЕ задачи пула уже есть" in result.stderr:
        # Формат кандидата — буквально `#<num> (score <s>): <title> — <url>`
        # (scripts/gh/issue-create, awk-строка после дедупликации #566);
        # голый "#(\d+)" ловил бы и номер класса дефекта в описании ошибки
        # ("класс #566, живой случай #518/…") ВМЕСТО номера кандидата.
        numbers = re.findall(r"#(\d+)\s*\(score", result.stderr)
        if numbers:
            existing = int(numbers[0])
            note = (f"Новая улика (без второй задачи — гвардия дублей #566 нашла эту "
                     f"открытой): «{resource_label}» снова {_fmt(current)}/{_fmt(limit)} "
                     f"({pct}%), {now}")
            try:
                pulse_guard.post_issue_comment(repo, existing, note)
            except RuntimeError as error:
                return existing, f"задача #{existing} уже открыта, комментарий НЕ оставлен: {error}"
            return existing, f"задача #{existing} уже открыта — добавлена новая улика"
    return None, f"issue-create отказал: {result.stderr.strip()[:400]}"


def check_and_alert(repo: str, resource_key: str, resource_label: str,
                     current: float, limit: float, pct: float, threshold: float = 80.0) -> str:
    """Единая точка входа обоих вызывающих (см. докстринг модуля). Возвращает
    строку для лога прогона — вызывающий печатает её и решает про exit code
    по той же подстроке "НЕ доставлен"/"НЕ оставлен", что и quotas.py.

    Первое наблюдение ресурса (prev_state is None), заставшее его уже в
    норме, — не переход breach→ok (события восстановления не было, эскалация
    сообщила бы о факте, которого не было), поэтому наружу тихо уходит только
    маркер состояния, без Telegram/следа-эскалации в #120 (найдено ревью
    PR #607 — до этой правки такой прогон слал ложное «квота вернулась ниже
    порога» на КАЖДЫЙ ресурс, впервые увиденный в норме)."""
    new_state = "breach" if pct >= threshold else "ok"
    prev_state, prev_issue = last_state(repo, resource_key)

    if prev_state is None and new_state == "ok":
        try:
            pulse_guard.post_issue_comment(repo, WATCHDOG_ISSUE, state_marker(resource_key, "ok", None))
        except RuntimeError as error:
            return (f"{resource_key}: первое наблюдение (ok, {pct}%) — маркер НЕ записан: {error}")
        return f"{resource_key}: первое наблюдение — состояние зафиксировано (ok, {pct}%)"

    if prev_state == new_state:
        return f"{resource_key}: без изменений ({new_state}, {pct}%) — сигнал не отправлен (дедуп)"

    if new_state == "breach":
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
        text += "\n" + state_marker(resource_key, "breach", issue_number)
        result = pulse_guard.escalate(repo, WATCHDOG_ISSUE, text)
        return f"{resource_key}: breach — {result}; {note}"

    text = (
        f"✅ edge-harness: квота «{resource_label}» вернулась ниже {threshold}% "
        f"({_fmt(current)} / {_fmt(limit)}, {pct}%). "
        + (f"Задача на разбор причины остаётся открытой: #{prev_issue}." if prev_issue
           else "Прежней задачи на разбор в маркере не найдено.")
        + "\n" + state_marker(resource_key, "ok", prev_issue)
    )
    result = pulse_guard.escalate(repo, WATCHDOG_ISSUE, text)
    return f"{resource_key}: recovery — {result}"


if __name__ == "__main__":
    print(__doc__)
    sys.exit(0)
