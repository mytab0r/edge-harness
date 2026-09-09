#!/usr/bin/env python3
"""Гвардия «задача ждёт решения владельца» (`waiting:owner`, #470, слайс #254).

Класс проблемы (живой случай #370): задача, чей критерий требует явного выбора
владельца между несколькими путями («владелец выбирает одно из трёх, не
молчаливым дефолтом»), лежала в открытом пуле, пока реализация одного из
вариантов не была слита ночью (PR #382) — владелец узнал об этом только
потому, что спросил сам. Признак «ждёт владельца» существовал только прозой в
теле задачи; ни одна автоматика его не видела и не эскалировала.

#254 — родительское предложение полного протокола («варианты в задаче, кнопки
в Telegram, ответ записан в репозиторий»). На момент ЭТОЙ задачи (#470)
интерактивные кнопки были недостижимы: Telegram-вебхук с авторизацией
`callback_query` в репозитории отсутствовал (`cf-worker/src/harness.ts` прямо
называл это отдельным нерешённым вопросом — инбокс был админско-релейным, не
прямым вебхуком). Эта гвардия реализовывала достижимый слайс без кнопок:
#254 сам называет комментарий-ответ полноценной заменой нажатию (п.4
предложения) — «тот же результат достигается комментарием в задаче руками».
Вебхук с `callback_query` добавлен позже, тем же #254 (PR #486): эскалация
ниже теперь передаёт `options` в `pulse_guard.escalate` (см.
`variant_option_labels`) — при машиночитаемом блоке вариантов сообщение
уходит с инлайн-кнопками, ручной ответ комментарием остаётся равноправным
путём (п.4 предложения не отменяется кнопками, а дополняется ими).

Три действия одного прогона, независимые и идемпотентные:

1. Авто-метка (`find_candidates_for_auto_label`): открытая задача с меткой
   `task`, ещё без `waiting:owner`, чьё тело несёт машиночитаемый блок
   «## Варианты владельца» с ≥MIN_VARIANTS пронумерованными строками формата
   «N. фраза — последствие» (docs/agents/PROTOCOL.md) — получает метку
   автоматически. Метку может поставить и человек руками при заведении
   задачи (docs/agents/LABELS.md, «кто ставит») — эта проверка лишь ловит
   случай, когда формат вариантов уже машиночитаем, а метку забыли.
2. Разрешение (`decision_marker`): задача с `waiting:owner`, в чьей истории
   (тело, затем комментарии хронологически) есть комментарий, начинающийся
   строкой «РЕШЕНИЕ: N» — владелец ответил. Метка снимается автоматически,
   подтверждающий комментарий ставится тем же прогоном. Последний по порядку
   маркер решает (тот же приём, что `stale_blocked_guard.current_stale_
   marker_target` уже применяет к «Блокирована: #N») — владелец мог
   поправить свой ответ вторым комментарием.
3. Эскалация (`escalation_pending`): задача с `waiting:owner` без маркера
   решения — сигнал в Telegram + комментарий В САМУ задачу (`pulse_guard.
   escalate`, второй канал не заводится — тот же приём, что уже применяет
   `stale_blocked_guard.py`/`upstream_drift.py`), идемпотентно на эпизод:
   маркер `WAITING_OWNER_ESCALATE_MARKER`, повтор — не раньше
   `WAITING_OWNER_REESCALATE_HOURS` часов с последнего сигнала (тот же порог,
   что `STALE_HOURS` в `scheduler.py::mark_stale_unclaimed` уже использует для
   «нет ответа сутки» — второй порог правды не заводится, обоснование там же:
   реже — владелец не узнает вовремя; чаще — эскалация каждые 15 минут пульса
   завалит канал). Машиночитаемый блок вариантов есть — `escalate` получает
   `options` (`variant_option_labels`), сообщение уходит с инлайн-кнопками
   (#254, PR #486); нет — обычный текстовый алерт, как раньше.

Тормоз для воркера — не здесь: `scripts/lib/claim_task.py::claim` отказывает
в аренде задачи с `waiting:owner` тем же путём, что уже отказывает `blocked`
(критерий #254 п.4, «задача не берётся воркером в работу»).

Честный потолок: признак вариантов и признак решения — узкие регэкспы по
одному конкретному формату (как `stale_blocked_guard.STALE_MARKER_RE` для
«Блокирована: #N»), не разбор произвольной прозы. Задача без машиночитаемого
блока вариантов размечается только вручную — это законный путь (п.1 выше),
не пропуск.

Запуск: python scripts/orchestra/waiting_owner_guard.py
Тесты: python -m pytest scripts/orchestra/test_waiting_owner_guard.py -q
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
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pulse_guard
from pulse_guard import escalate

_LIB = Path(__file__).resolve().parents[1] / "lib"
_rl_spec = importlib.util.spec_from_file_location("review_labels", _LIB / "review_labels.py")
review_labels = importlib.util.module_from_spec(_rl_spec)
_rl_spec.loader.exec_module(review_labels)

WAITING_OWNER_LABEL = "waiting:owner"

# Блок вариантов (docs/agents/PROTOCOL.md): заголовок ровно на своей строке,
# дальше — пронумерованные строки «N. фраза — последствие» до следующего `## `
# заголовка или конца тела. Тире — «—» (em-dash), как везде в прозе этого
# репозитория (см. сам этот докстринг), не дефис — узкий признак, случайный
# дефис в фразе варианта не матчит.
VARIANTS_HEADER_RE = re.compile(r"^##\s*Варианты владельца\s*$", re.MULTILINE)
VARIANT_LINE_RE = re.compile(r"^\s*\d+\.\s+\S.*—.*\S\s*$", re.MULTILINE)
NEXT_HEADER_RE = re.compile(r"^##\s", re.MULTILINE)
MIN_VARIANTS = 2

# Разбор ОДНОЙ строки варианта на номер + подпись кнопки (#254, PR #486;
# нумерация — находка ревью PR #486, второй заход): фраза БЕЗ номера и БЕЗ
# последствия, номер — ОТДЕЛЬНОЙ группой. build_decision_keyboard нумерует
# callback_data позицией в списке `options`, а PROTOCOL.md не гарантирует,
# что письменные номера вариантов идут подряд 1..N (требует лишь «не меньше
# двух пронумерованных строк») — если бы labels собирались без сверки с
# написанным номером, кнопка «2-я по счёту» могла бы записать «РЕШЕНИЕ: 2»
# для варианта, который в тексте назван «3.». Группа 1 — номер, используется
# variant_option_labels() для проверки подряд идущей нумерации ДО передачи в
# build_decision_keyboard, не для самой сборки клавиатуры (та нумерует
# положением — не второй формат, тот же VARIANT_LINE_RE, шире на группы).
VARIANT_LABEL_RE = re.compile(r"^\s*(\d+)\.\s+(.+?)\s*—")

# Маркер ответа владельца: «РЕШЕНИЕ: N» первой строкой комментария (по образцу
# «Блокирована: #N» из stale_blocked_guard.py — colon сразу после слова,
# единственная форма, распознаваемая как решение, не случайное упоминание
# слова «решение» в прозе обсуждения).
DECISION_MARKER_RE = re.compile(r"^\s*РЕШЕНИЕ\s*:\s*(\d+)\b", re.IGNORECASE | re.MULTILINE)

# Маркер эпизода эскалации ЭТОЙ гвардии (не путать с DECISION_MARKER_RE — тем
# маркером отвечает владелец, этим гвардия называет свой сигнал). Форма — как
# STALE_ESCALATE_MARKER/PAUSE_MARKER: скобочная строка, в прозе так не пишут.
WAITING_OWNER_ESCALATE_MARKER = "[решение владельца: запрошено]"

# Порог повтора эскалации — тот же класс «нет ответа сутки», что уже действует
# в репозитории (scheduler.STALE_HOURS для stale-unclaimed, #427): реже —
# владелец не узнает вовремя; каждые 15 минут пульса — завалит канал.
WAITING_OWNER_REESCALATE_HOURS = 24


# ── Чистое решение: блок вариантов ───────────────────────────────────────────


def variant_lines(body: str) -> list[str]:
    """Пронумерованные строки варианта из блока «## Варианты владельца» —
    только между заголовком и следующим `## ` (или концом тела). Пустой
    список — блока нет вовсе, либо в нём нет ни одной строки нужного формата."""
    header = VARIANTS_HEADER_RE.search(body or "")
    if not header:
        return []
    tail = body[header.end():]
    next_header = NEXT_HEADER_RE.search(tail)
    section = tail[:next_header.start()] if next_header else tail
    return [match.group(0).strip() for match in VARIANT_LINE_RE.finditer(section)]


def variant_option_labels(body: str) -> list[str]:
    """Короткие подписи вариантов для инлайн-кнопок Telegram (#254, PR #486).
    build_decision_keyboard нумерует callback_data ПОЗИЦИЕЙ в возвращаемом
    списке (1, 2, 3…) — это ОБЯЗАНО совпасть с номером, который владелец
    видит в тексте варианта и называет в ответе «РЕШЕНИЕ: N», иначе нажатие
    кнопки «второй по счёту» запишет решение за вариант, названный в тексте
    «3.» (находка ревью PR #486, второй заход — живой пример: блок «1. …» /
    «3. …», два варианта — валиден по PROTOCOL.md, «не меньше двух
    пронумерованных строк» не требует непрерывности). Проверка: письменные
    номера (группа 1 VARIANT_LABEL_RE), собранные по порядку появления,
    обязаны быть РОВНО `1, 2, …, len(строк)` — иначе кнопки не строятся вовсе,
    пустой список (escalate() честно падает на текстовый алерт, тот же путь,
    что и для задач без машиночитаемого блока — «владелец ответит текстом»,
    п.4 предложения #254, а не полумеры с неверной нумерацией)."""
    lines = variant_lines(body)
    parsed = [VARIANT_LABEL_RE.match(line) for line in lines]
    if not all(parsed):
        return []
    numbers = [int(match.group(1)) for match in parsed]
    if numbers != list(range(1, len(numbers) + 1)):
        return []
    return [match.group(2).strip() for match in parsed]


def should_auto_label(body: str) -> bool:
    """≥MIN_VARIANTS вариантов в машиночитаемом блоке — задача годится для
    авто-метки без участия человека."""
    return len(variant_lines(body)) >= MIN_VARIANTS


def find_candidates_for_auto_label(issues: list[dict]) -> list[dict]:
    """Открытые `task`-issue без `waiting:owner`, чьё тело несёт блок
    вариантов — кандидаты на авто-метку."""
    candidates = []
    for issue in issues:
        labels = {label["name"] for label in issue.get("labels") or []}
        if WAITING_OWNER_LABEL in labels:
            continue
        if should_auto_label(issue.get("body") or ""):
            candidates.append(issue)
    return candidates


# ── Чистое решение: маркер ответа владельца ──────────────────────────────────


def decision_marker(texts: list[str]) -> int | None:
    """Номер варианта из ПОСЛЕДНЕГО (по порядку — тело, затем комментарии
    хронологически) текста с маркером «РЕШЕНИЕ: N» — тот же приём «последний
    маркер решает», что stale_blocked_guard.current_stale_marker_target уже
    применяет к «Блокирована: #N»: владелец мог поправить ответ вторым
    комментарием. None — маркера нет вовсе."""
    found: int | None = None
    for text in texts:
        match = DECISION_MARKER_RE.search(text or "")
        if match:
            found = int(match.group(1))
    return found


def find_resolved(issues: list[dict]) -> list[dict]:
    """Issue с `waiting:owner` (несущие ключ `comments_text`, собранный IO-
    обвязкой), где найден маркер решения — метка готова к снятию."""
    resolved = []
    for issue in issues:
        texts = [issue.get("body") or ""] + list(issue.get("comments_text") or [])
        option = decision_marker(texts)
        if option is not None:
            resolved.append({"number": issue["number"], "option": option})
    return resolved


def resolved_comment_text(option: int) -> str:
    return (f"✅ решение владельца получено (вариант {option}) — метка "
            f"`{WAITING_OWNER_LABEL}` снята, задача открыта для работы по "
            "выбранному варианту.")


# ── Чистое решение: эскалация ────────────────────────────────────────────────


def escalation_pending(marker_times: list[datetime], now: datetime,
                       threshold_hours: float = WAITING_OWNER_REESCALATE_HOURS) -> bool:
    """Эскалировать нужно, если маркера этого эпизода ещё не было, либо
    последний старше порога — идемпотентность на 15-минутный пульс."""
    if not marker_times:
        return True
    age_minutes = pulse_guard.minutes_between(max(marker_times), now)
    return age_minutes >= threshold_hours * 60


def waiting_owner_alert_text(repo: str, issue: dict) -> str:
    """Текст эскалации: одной фразой — что решить, варианты с последствием
    (или честная оговорка, если блок вариантов не размечен машиночитаемо),
    как ответить, ссылка на задачу, порог повтора — план, не голая
    констатация (правило репозитория «алерт обязан кончаться планом»)."""
    variants = variant_lines(issue.get("body") or "")
    variants_block = "\n".join(variants) if variants else (
        "(варианты не размечены машиночитаемым блоком — смотри тело задачи по ссылке)")
    title = (issue.get("title") or "").strip()
    return (
        f"🧭 {WAITING_OWNER_ESCALATE_MARKER}\n"
        f"Нужен твой выбор: {title}\n"
        f"{variants_block}\n"
        "Ответь комментарием в задаче первой строкой «РЕШЕНИЕ: <номер>».\n"
        f"https://github.com/{repo}/issues/{issue['number']}\n"
        f"Без ответа — следующее напоминание не раньше {WAITING_OWNER_REESCALATE_HOURS} ч."
    )


# ── Тонкая IO-обвязка ─────────────────────────────────────────────────────────


def open_task_issues(repo: str) -> list[dict]:
    """Открытые issues (не PR) с меткой `task` — источник кандидатов авто-метки."""
    issues = review_labels.list_pages(
        f"repos/{repo}/issues?state=open&labels=task&per_page=100", pulse_guard.gh)
    return [issue for issue in issues if "pull_request" not in issue]


def open_waiting_owner_issues(repo: str) -> list[dict]:
    """Открытые issues (не PR) с меткой `waiting:owner`."""
    issues = review_labels.list_pages(
        f"repos/{repo}/issues?state=open&labels={WAITING_OWNER_LABEL}&per_page=100",
        pulse_guard.gh)
    return [issue for issue in issues if "pull_request" not in issue]


def fetch_comments(repo: str, number: int) -> list[dict]:
    return review_labels.list_pages(
        f"repos/{repo}/issues/{number}/comments?per_page=100", pulse_guard.gh)


def add_label(repo: str, number: int) -> None:
    pulse_guard.gh("-X", "POST", f"repos/{repo}/issues/{number}/labels",
                   "-f", f"labels[]={WAITING_OWNER_LABEL}")


def remove_label(repo: str, number: int) -> None:
    pulse_guard.gh("-X", "DELETE",
                   f"repos/{repo}/issues/{number}/labels/{WAITING_OWNER_LABEL}")


def waiting_owner_check(repo: str, now: datetime) -> list[str]:
    """Проводка: один живой прогон, вызывается из main() ниже (и способен
    вызываться отдельным шагом workflow, по образцу stale_blocked_guard.py —
    не завязан на scheduler.py, который правится параллельно другим агентом)."""
    lines: list[str] = []

    pool = open_task_issues(repo)
    candidates = find_candidates_for_auto_label(pool)
    for issue in candidates:
        try:
            add_label(repo, issue["number"])
            lines.append(f"🏷️ #{issue['number']}: waiting:owner проставлена автоматически "
                         "(найден блок «## Варианты владельца»)")
        except RuntimeError as error:
            print(f"::warning::метка waiting:owner не поставлена на #{issue['number']}: {error}",
                  file=sys.stderr)
            # Симметрично провалу remove_label ниже (находка AI-ревью PR #471):
            # молчаливое «::warning:: и продолжаем» без строки отчёта могло
            # оставить прогон вовсе без строк (холостое 💗 не печатается,
            # раз candidates непуст) — провал постановки не должен быть тише
            # провала снятия.
            lines.append(f"🚨 #{issue['number']}: найден блок вариантов, но метка "
                         f"waiting:owner НЕ поставлена: {error}")

    labeled = open_waiting_owner_issues(repo)
    if not labeled and not candidates:
        lines.append(f"💗 {WAITING_OWNER_LABEL}: открытых задач с меткой нет")

    comments_by_number: dict[int, list[dict]] = {}
    for issue in labeled:
        comments = fetch_comments(repo, issue["number"])
        comments_by_number[issue["number"]] = comments
        issue["comments_text"] = [comment.get("body") or "" for comment in comments]

    # find_resolved — то же чистое решение, что уже покрыто юнит-тестами
    # (находка AI-ревью PR #471: не дублируем его логику инлайн здесь).
    resolved_options = {item["number"]: item["option"] for item in find_resolved(labeled)}

    for issue in labeled:
        number = issue["number"]
        comments = comments_by_number[number]
        if number in resolved_options:
            option = resolved_options[number]
            try:
                remove_label(repo, number)
            except RuntimeError as error:
                print(f"::warning::метка waiting:owner не снята с #{number}: {error}",
                      file=sys.stderr)
                lines.append(f"🚨 #{number}: решение получено (вариант {option}), "
                             f"но метка НЕ снята: {error}")
                continue
            try:
                pulse_guard.post_issue_comment(repo, number, resolved_comment_text(option))
            except RuntimeError as error:
                print(f"::warning::подтверждение не оставлено в #{number}: {error}",
                      file=sys.stderr)
            lines.append(f"✅ #{number}: решение получено (вариант {option}), метка снята")
            continue

        marker_times = [
            pulse_guard.parse_time(comment["created_at"]) for comment in comments
            if WAITING_OWNER_ESCALATE_MARKER in (comment.get("body") or "")
        ]
        if escalation_pending(marker_times, now):
            text = waiting_owner_alert_text(repo, issue)
            # options (#254, PR #486): непустой список — Telegram-сообщение
            # уходит с инлайн-кнопками (webhook callback_query теперь принят
            # мордой, PR #486); блока вариантов нет машиночитаемо — escalate()
            # честно падает обратно на текстовый алерт (см. её докстринг).
            options = variant_option_labels(issue.get("body") or "")
            delivered = escalate(repo, issue["number"], text, options=options)
            lines.append(f"🚨 #{issue['number']}: нужен выбор владельца — сигнал ({delivered})")
        else:
            lines.append(f"🔇 #{issue['number']}: нужен выбор владельца "
                         "(уже сигналили, следующее напоминание позже)")

    return lines


def main() -> int:
    repo = os.environ.get("GITHUB_REPOSITORY", "mytab0r/edge-harness")
    now = datetime.now(timezone.utc)
    lines = waiting_owner_check(repo, now)
    for line in lines:
        print(line)
    # 🔇 (эпизод уже эскалирован, повтор не шлём) — это всё ещё незакрытое
    # ожидание, не холостой ход: молчит только канал эскалации (тот же приём,
    # что stale_blocked_guard.main). 🏷️/✅ — выполненные действия, не находки.
    return 0 if all(not line.startswith("🚨") and not line.startswith("🔇") for line in lines) else 1


if __name__ == "__main__":
    sys.exit(main())
