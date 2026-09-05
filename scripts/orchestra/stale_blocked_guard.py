#!/usr/bin/env python3
"""Гвардия «протухшей» метки `blocked`: причина закрылась, метка осталась (#334).

Класс проблемы (AGENTS.md, «Решение — это механизм, а не текст»): эскалация
воркера называет газ метки `blocked` текстом в комментарии — «снимет владелец,
когда закроется #N» (docs/agents/LABELS.md, строка `blocked`) — но НИЧТО не
проверяет, что это условие действительно наступило и метка правда снята.
Решение объявлено прозой и живёт, пока кто-то не заметит его вручную.

Живой замер (не гипотетический — обнаружен при заведении #334): issue #268
несёт метку `blocked`, эскалационный комментарий прямо называет причину и
газ первой строкой — «Блокирована: #265» (полный текст комментария называет
и способ проверки: «слияние PR #265 … переход в open state в исходной задаче
#265»). #265 закрыт 2026-09-05T13:33:09Z; метка `blocked` на #268 к моменту
завода этой задачи не снята ни автоматикой (её нет), ни владельцем (не
заметил) — ровно тот класс, который правило называет.

Признак — УЗКИЙ, не «любое упоминание #N в тексте». Первая попытка (любое
упоминание, включая тело задачи) на тех же живых данных дала бы ложное
срабатывание: issue #216 в теле называет три номера («PR #162 … #163 …
#164») с союзом И — блокировка снимается, когда слиты ВСЕ три, а не любой
один; #163/#164 уже закрыты, #162 ещё открыт, блокировка на #216 законна и
СЕЙЧАС. Разбирать многономерные конъюнкции прозы — дорого и хрупко. Дешёвый и
надёжный признак — маркерная фраза «Блокирована: #N» первой строкой
эскалационного текста (по образцу маркеров эпизода pulse_guard/
upstream_drift: «[дрейф пина: …]», PAUSE_MARKER) — называет РОВНО ОДНУ
причину без союзов; issue #216 такую фразу не использует (метка поставлена
не эскалацией task.sh, а автором issue при заведении) — под признак не
попадает, ложного срабатывания на живых данных нет (доказано тестом на
прод-форме тела #216 ниже).

Честный потолок: эскалации, использующие другую формулировку («Блокировано
из-за …», «см. #N» без маркера) — не ловятся. Это признанная неполнота, а не
скрытая: новый маркер добавляется правкой STALE_MARKER_RE, одно место.

Эта гвардия НЕ снимает метку сама и не решает вместо владельца (LABELS.md:
газ `blocked` — «вручную: владелец», это решение здесь не пересматривается).
Она делает протухание ВИДИМЫМ: issue с меткой `blocked`, чей текст называет
маркером ровно один блокирующий issue/PR `#N`, а тот `#N` уже CLOSED —
попадает в отчёт как нарушение.

Находка ревью #333/#336: печать строки в лог шага с `continue-on-error: true`
(.github/workflows/orchestra.yml) — тот же класс, что «Тормоз без газа» из
AGENTS.md: механизм проверки есть, а носитель доставки сигнала до владельца —
нет (лог этого job'а уже однажды никто не читал, #268 висел незамеченным).
Поэтому здесь тот же канал, что у upstream_drift/pulse_guard: эскалация —
комментарий В САМ протухший issue (не в отдельную задачу-статус, у каждого
`#N` своя) + Telegram (pulse_guard.escalate), один раз на эпизод. Эпизод —
маркер `STALE_ESCALATE_MARKER` с целью «набор протухших ссылок»: та же цель
уже сигналилась в этом issue — молчим (иначе 15-минутный крон заспамил бы
канал), набор изменился (новая ссылка протухла, старая ушла) — сигналим
заново. Комментарии для проверки маркера уже собраны выше
(`fetch_comments_text`) — второго сетевого похода за тем же issue не нужно.

Признак — свой, узкий регэксп STALE_MARKER_RE, не task_ref.extract_task_refs
(широкая семантика «любое упоминание #N» — ровно то, от чего этот модуль
отказался выше, увидев ложное срабатывание на #216).

Запуск живого прогона: python scripts/orchestra/stale_blocked_guard.py
Запуск тестов: python -m pytest scripts/orchestra/test_stale_blocked_guard.py -q
"""

import importlib.util
import os
import re
import sys
from pathlib import Path

import pulse_guard
from pulse_guard import escalate

_LIB = Path(__file__).resolve().parents[1] / "lib"

_rl_spec = importlib.util.spec_from_file_location("review_labels", _LIB / "review_labels.py")
review_labels = importlib.util.module_from_spec(_rl_spec)
_rl_spec.loader.exec_module(review_labels)

BLOCKED_LABEL = "blocked"

# Маркер эскалации: «Блокирована: #N» (и формы «Блокирован»/«Блокировано» —
# согласование рода темы). Colon сразу перед #N — единственная форма,
# распознаваемая как ОДНА названная причина, не перечисление в прозе.
STALE_MARKER_RE = re.compile(r"Блокирован[а-я]*\s*:\s*#(\d+)", re.IGNORECASE)

# Маркер эскалации ЭТОЙ гвардии (не путать со STALE_MARKER_RE — тем маркером
# воркер называет причину блокировки; этим гвардия называет свою находку).
# Форма — как DRIFT_MARKER/PAUSE_MARKER: скобочная строка, в прозе так не
# пишут, тело — цель эпизода (набор протухших ссылок), закрывается «]».
STALE_ESCALATE_MARKER = "[протухшая блокировка:"


# ── Чистое решение ────────────────────────────────────────────────────────────


def stale_marker_targets(own_number: int, texts: list[str]) -> list[int]:
    """Номера, названные маркером «Блокирована: #N» в текстах (тело +
    комментарии), без повторов и без ссылки на себя. Граница числа с обеих
    сторон (класс #187 — «#18» не должен матчить «#180») здесь не нужна
    отдельным регэкспом task_ref: `\\d+` в STALE_MARKER_RE уже жадный и
    захватывает ВЕСЬ прогон цифр целиком, а слева от `#` в маркере всегда
    буква/двоеточие/пробел, не цифра — подстрочное слипание чисел здесь
    структурно невозможно."""
    numbers: list[int] = []
    seen: set[int] = {own_number}
    for text in texts:
        for match in STALE_MARKER_RE.finditer(text or ""):
            number = int(match.group(1))
            if number in seen:
                continue
            seen.add(number)
            numbers.append(number)
    return numbers


def find_stale_blocked(issues: list[dict], closed_numbers: set[int]) -> list[dict]:
    """issues — прод-форма repos/{repo}/issues, дополненная ключом
    `comments_text: list[str]` (тела комментариев, собранные IO-обвязкой ниже).
    closed_numbers — номера issue/PR, которые сейчас CLOSED.

    Issue без метки `blocked` — не наш случай. Issue с меткой, но без маркера
    «Блокирована: #N» в тексте — законный ручной случай (LABELS.md) или форма
    вне признака (честный потолок в докстринге модуля), не нарушение. Issue с
    маркером на закрытый номер — протухшая блокировка, входит в отчёт."""
    violations = []
    for issue in issues:
        labels = {label["name"] for label in issue.get("labels") or []}
        if BLOCKED_LABEL not in labels:
            continue
        number = issue["number"]
        texts = [issue.get("body") or ""] + list(issue.get("comments_text") or [])
        targets = stale_marker_targets(number, texts)
        stale = sorted(n for n in targets if n in closed_numbers)
        if stale:
            violations.append({"number": number, "stale_refs": stale})
    return violations


def violation_text(violation: dict) -> str:
    refs = ", ".join(f"#{n}" for n in violation["stale_refs"])
    return (
        f"#{violation['number']}: метка `blocked` стоит, но названная маркером "
        f"«Блокирована: {refs}» причина уже закрыта — газ объявлен, но не "
        "проверен; владелец: снять метку или назвать актуальную причину (#334)"
    )


def escalation_target(violation: dict) -> str:
    """Цель эпизода — набор протухших ссылок, стабильно отсортированный
    (одинаковый набор при повторном прогоне даёт тот же маркер)."""
    return ",".join(f"#{n}" for n in violation["stale_refs"])


def escalation_marker_target(body: str) -> str | None:
    """Тело маркера STALE_ESCALATE_MARKER из текста комментария, если он там
    есть (по образцу upstream_drift.last_drift_target)."""
    start = body.find(STALE_ESCALATE_MARKER)
    if start == -1:
        return None
    end = body.find("]", start)
    if end == -1:
        return None
    return body[start + len(STALE_ESCALATE_MARKER):end].strip()


def already_escalated(texts: list[str], target: str) -> bool:
    """True — этот же эпизод (тот же набор протухших ссылок) уже сигналился
    в issue: маркер с таким же телом среди уже прочитанных текстов есть."""
    return any(escalation_marker_target(text or "") == target for text in texts)


def escalation_text(violation: dict) -> str:
    """Текст эскалации: первая строка — маркер эпизода (как
    drift_alert_text/pause_alert_text), дальше — уже готовый violation_text."""
    return f"🚨 {STALE_ESCALATE_MARKER} {escalation_target(violation)}]\n{violation_text(violation)}"


# ── Тонкая IO-обвязка ─────────────────────────────────────────────────────────


def open_blocked_issues(repo: str) -> list[dict]:
    """Открытые issues (не PR) с меткой `blocked`, обход страниц (класс #308)."""
    issues = review_labels.list_pages(
        f"repos/{repo}/issues?state=open&labels={BLOCKED_LABEL}&per_page=100",
        pulse_guard.gh,
    )
    return [issue for issue in issues if "pull_request" not in issue]


def fetch_comments_text(repo: str, number: int) -> list[str]:
    comments = review_labels.list_pages(
        f"repos/{repo}/issues/{number}/comments?per_page=100", pulse_guard.gh)
    return [comment.get("body") or "" for comment in comments]


def is_closed(repo: str, number: int) -> bool:
    """Состояние issue ИЛИ PR по общему issues-эндпоинту (PR доступен через
    него же) — нам важно только open/closed, не merged отдельно."""
    data = pulse_guard.gh(f"repos/{repo}/issues/{number}")
    return bool(data) and data.get("state") == "closed"


def stale_blocked_check(repo: str) -> list[str]:
    """Проводка: один живой прогон. Возвращает строки отчёта (пустой список —
    холостой ход, ни одной протухшей блокировки не найдено)."""
    issues = open_blocked_issues(repo)
    for issue in issues:
        issue["comments_text"] = fetch_comments_text(repo, issue["number"])

    referenced: set[int] = set()
    for issue in issues:
        texts = [issue.get("body") or ""] + issue["comments_text"]
        referenced.update(stale_marker_targets(issue["number"], texts))

    closed_numbers = {number for number in referenced if is_closed(repo, number)}
    violations = find_stale_blocked(issues, closed_numbers)
    if not violations:
        return [f"💗 blocked: протухших меток не найдено ({len(issues)} issue с меткой blocked проверено)"]

    by_number = {issue["number"]: issue for issue in issues}
    lines = []
    for violation in violations:
        issue = by_number[violation["number"]]
        texts = [issue.get("body") or ""] + issue["comments_text"]
        target = escalation_target(violation)
        if already_escalated(texts, target):
            # Тот же набор протухших ссылок уже сигналился в этом issue —
            # молчим (иначе 15-минутный крон заспамил бы канал).
            lines.append(f"🔇 {violation_text(violation)} (уже эскалировано в этом эпизоде)")
            continue
        delivered = escalate(repo, violation["number"], escalation_text(violation))
        lines.append(f"🚨 {violation_text(violation)} (сигнал: {delivered})")
    return lines


def main() -> int:
    repo = os.environ.get("GITHUB_REPOSITORY", "mytab0r/edge-harness")
    lines = stale_blocked_check(repo)
    for line in lines:
        print(line)
    # 🔇 (тот же эпизод уже эскалирован, повтор не шлём) — тоже нарушение, не
    # холостой ход: молчим только каналом эскалации, не CI-статусом. Красный
    # шаг остаётся видимым при continue-on-error (находка ревью #333/#336) —
    # ровно тот второй способ («видно красным шагом»), которым LABELS.md
    # теперь и описывает поведение.
    return 0 if all(line.startswith("💗") for line in lines) else 1


if __name__ == "__main__":
    sys.exit(main())
