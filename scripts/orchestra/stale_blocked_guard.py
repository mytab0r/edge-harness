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

Вторая живая формулировка (найдена при разборе issue #938, тот же класс, что
и сам #938 — «формат маркера задан регэкспом в одном месте, а пишут его
иначе»): комментарии на #215 и #258 (воркер выводит сессию из ротации при
провале холодной загрузки, #794/#809) называют причину блокировки строкой
«Причина блокировки: #809» — не «Блокирована: #265». Оба факта поданы
источником, который у этой гвардии нет (комментарий пишет человек/сессия
руками, не шаблон из scripts/): #809 закрылся 2026-09-09, метка `blocked` на
#215/#258 не снята никем, потому что STALE_MARKER_RE узнавал только «Блокиро-
ван[а-я]*: #N». STALE_MARKER_RE теперь принимает ОБЕ формы — «Блокирован[а-
я]*: #N» и «Причина блокировки: #N» — одним регэкспом, не второй копией.

Честный потолок: эскалации, использующие ЛЮБУЮ другую формулировку
(«Блокировано из-за …», «см. #N» без маркера) — не ловятся. Это признанная
неполнота, а не скрытая: новый маркер добавляется правкой STALE_MARKER_RE,
одно место.

Пересмотрено #1111/#1157 (класс «тормоз ставится автоматически, снимается
только руками», живой случай: #215/#258 несут «Причина блокировки: #809»,
#809 закрыт 2026-09-09/10, метка не снята никем ни автоматикой (её не было),
ни владельцем — снял бы только сплошной прочёс). Прежнее решение («не снимает
метку сама») держалось на прозе «не решает вместо владельца» — эта причина
НЕ выдерживает проверки для УЗКОГО признака этого модуля: закрытость issue,
названного маркером «Блокирована: #N»/«Причина блокировки: #N», —
факт (`state == "closed"` по живому GET), а не суждение. Суждение (трактовка
находки, выбор приоритета) — то, что AGENTS.md прямо оставляет человеку;
«закрыт ли номер X» им не является. Поэтому для ЭТОГО узкого признака гвардия
теперь снимает метку сама и оставляет след — комментарий, называющий закрытую
задачу и когда она закрылась (см. `removal_comment_text`).

Учтён риск (не совпадает с прежним классом честного потолка выше): блокировка
может быть поставлена БЕЗ ссылки на другой issue («упёрся в то, что есть
только у владельца — секрет/доступ/деньги», LABELS.md, живой пример в тестах
ниже — issue без единого маркера). `find_stale_blocked` такие не находит
вовсе (нет маркера — нет `current_stale_marker_target`) — они остаются
ручными в точности как раньше, это не задевается правкой ни строкой кода.

Газ у этого газа (если снятие ошиблось — например, `#N` закрыт как
`not_planned`, а реальная причина блокировки жива): снятие метки тривиально
обратимо тем же путём, что и раньше — `gh issue edit <N> --add-label blocked`
плюс новый маркер «Блокирована: #M»/«Причина блокировки: #M» переустанавливает
легитимную блокировку; `current_stale_marker_target` уже учитывает только
ПОСЛЕДНИЙ по порядку маркер (переустановка не путается со старым эпизодом).
Комментарий снятия называет закрытый номер и время — у того, кто заметит
ошибку, есть все данные без раскопок истории.

Находка ревью #333/#336 (сохраняется для оставшихся ручных случаев и для
провала самого снятия): печать строки в лог шага с `continue-on-error: true`
(.github/workflows/orchestra.yml) — тот же класс, что «Тормоз без газа» из
AGENTS.md: механизм проверки есть, а носитель доставки сигнала до владельца —
нет (лог этого job'а уже однажды никто не читал, #268 висел незамеченным).
Провал самого снятия (сеть/права) эскалируется тем же каналом, что раньше
эскалировалось само протухание: комментарий В САМ issue + Telegram
(`pulse_guard.escalate`), один раз на эпизод, маркер `STALE_ESCALATE_MARKER`.

Признак — свой, узкий регэксп STALE_MARKER_RE, не task_ref.extract_task_refs
(широкая семантика «любое упоминание #N» — ровно то, от чего этот модуль
отказался выше, увидев ложное срабатывание на #216).

Запуск живого прогона: python scripts/orchestra/stale_blocked_guard.py
Запуск тестов: python -m pytest scripts/orchestra/test_stale_blocked_guard.py -q
"""

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
from pathlib import Path

import pulse_guard
from pulse_guard import escalate

_LIB = Path(__file__).resolve().parents[1] / "lib"

_rl_spec = importlib.util.spec_from_file_location("review_labels", _LIB / "review_labels.py")
review_labels = importlib.util.module_from_spec(_rl_spec)
_rl_spec.loader.exec_module(review_labels)

BLOCKED_LABEL = "blocked"

# Маркер эскалации: «Блокирована: #N» (и формы «Блокирован»/«Блокировано» —
# согласование рода темы) ЛИБО «Причина блокировки: #N» (живая формулировка
# #215/#258, issue #938 — тот же класс маркера, вторые слова). Colon сразу
# перед #N — единственная форма, распознаваемая как ОДНА названная причина,
# не перечисление в прозе.
#
# Позиция маркера — НАЧАЛО СТРОКИ (^ + MULTILINE, находка ревью PR #336: без
# неё регэксп матчил маркер в любом месте прозы — цитата старого маркера в
# новой реплике, «задача заблокирована: #N» внутри предложения — и превращал
# гвардию в cry-wolf, собственный эскалационный комментарий гвардии цитирует
# маркер) ЛИБО сразу после конца предыдущего предложения («. » — точка и
# пробел). Второе добавлено находкой issue #938: живой комментарий #215/#258
# несёт «Причина блокировки: #809.» ПОСЛЕДНИМ предложением одного абзаца без
# переноса строки — жёсткая привязка «только начало строки» не поймала бы ни
# одного реального случая этой формулировки, сделав её поддержку бесполезной.
# Позиция «после точки с пробелом» не расширяет cry-wolf риск теста выше:
# цитата «было «Блокирована: #265»» стоит после «: было «» (двоеточие и
# открывающая кавычка, не точка+пробел) — не матчит ни старым, ни новым
# условием.
STALE_MARKER_RE = re.compile(
    r"(?:^|(?<=\.\s))\s*(?:Блокирован[а-я]*|Причина блокировки)\s*:\s*#(\d+)",
    re.IGNORECASE | re.MULTILINE)

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
    структурно невозможно.

    Это ВСЯ история упоминаний, не только текущая причина (см.
    `current_stale_marker_target` ниже для решения о нарушении) —
    используется только чтобы знать, чьё состояние (open/closed) вообще
    стоит проверить сетевым вызовом."""
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


def current_stale_marker_target(own_number: int, texts: list[str]) -> int | None:
    """Текущая причина блокировки — ПОСЛЕДНИЙ по порядку текст (тело, затем
    комментарии хронологически), несущий маркер «Блокирована: #N», решает,
    более ранние — не в счёт (находка AI-ревью PR #336, третий раунд).

    Живой контрпример, на котором старая семантика («любое упоминание в
    истории — нарушение, если номер закрыт») ловила ложное срабатывание:
    эпизод «Блокирована: #265» решён — #265 закрыт, метку сняли, — задача
    затем ЗАКОННО пере-блокирована новым эскалационным комментарием
    «Блокирована: #300» (#300 ещё открыт). Старая семантика продолжала бы
    видеть #265 в списке целей и красить шаг каждый прогон, хотя текущая
    причина легитимна и вообще другая. Последний маркер вытесняет прежние —
    ровно то же правило, что уже применяется к DRIFT_MARKER/PAUSE_MARKER
    эпизодам в pulse_guard/upstream_drift (актуально только последнее
    состояние маркера, не вся история)."""
    for text in reversed(texts):
        match = STALE_MARKER_RE.search(text or "")
        if match:
            number = int(match.group(1))
            if number != own_number:
                return number
    return None


def find_stale_blocked(issues: list[dict], closed_numbers: set[int]) -> list[dict]:
    """issues — прод-форма repos/{repo}/issues, дополненная ключом
    `comments_text: list[str]` (тела комментариев, собранные IO-обвязкой ниже).
    closed_numbers — номера issue/PR, которые сейчас CLOSED.

    Issue без метки `blocked` — не наш случай. Issue с меткой, но без маркера
    «Блокирована: #N» в тексте — законный ручной случай (LABELS.md) или форма
    вне признака (честный потолок в докстринге модуля), не нарушение. Issue,
    чья ТЕКУЩАЯ причина (последний маркер, `current_stale_marker_target`) —
    закрытый номер, протухшая блокировка, входит в отчёт. Более ранние,
    вытесненные маркеры в счёт не идут (находка AI-ревью PR #336, третий
    раунд, см. докстринг `current_stale_marker_target`)."""
    violations = []
    for issue in issues:
        labels = {label["name"] for label in issue.get("labels") or []}
        if BLOCKED_LABEL not in labels:
            continue
        number = issue["number"]
        texts = [issue.get("body") or ""] + list(issue.get("comments_text") or [])
        current = current_stale_marker_target(number, texts)
        if current is not None and current in closed_numbers:
            violations.append({"number": number, "stale_refs": [current]})
    return violations


def violation_text(violation: dict) -> str:
    refs = ", ".join(f"#{n}" for n in violation["stale_refs"])
    return (
        f"#{violation['number']}: метка `blocked` стояла из-за {refs} — "
        "уже закрыт(ы), условие снятия наступило"
    )


def removal_comment_text(violation: dict, states: dict[int, dict]) -> str:
    """Текст следа снятия (#1157): называет закрытую задачу и когда она
    закрылась — «какая задача закрыта, когда», как требует правило
    репозитория «Решение — это механизм, а не текст». `closed_by` (кем)
    добавляется, только если GitHub его вернул (полный ответ single-issue
    несёт это поле, список — нет; здесь всегда полный ответ, см. `issue_state`)."""
    parts = []
    for number in violation["stale_refs"]:
        data = states.get(number) or {}
        closed_at = data.get("closed_at") or "момент неизвестен (поле не вернулось)"
        closed_by = (data.get("closed_by") or {}).get("login")
        by_suffix = f", закрыл `{closed_by}`" if closed_by else ""
        parts.append(f"#{number} закрыт {closed_at}{by_suffix}")
    refs_text = "; ".join(parts)
    return (
        f"✅ метка `blocked` снята автоматически (stale_blocked_guard, #1157): "
        f"причина блокировки — {refs_text}. Если снятие ошибочно (блокировка "
        "действует по другой причине) — верните метку `gh issue edit "
        f"{violation['number']} --add-label blocked` с новым маркером "
        "«Блокирована: #M» / «Причина блокировки: #M»."
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
        f"repos/{repo}/issues?state=open&labels={review_labels.label_query_value(BLOCKED_LABEL)}"
        "&per_page=100",
        pulse_guard.gh,
    )
    return [issue for issue in issues if "pull_request" not in issue]


def fetch_comments_text(repo: str, number: int) -> list[str]:
    comments = review_labels.list_pages(
        f"repos/{repo}/issues/{number}/comments?per_page=100", pulse_guard.gh)
    return [comment.get("body") or "" for comment in comments]


def issue_state(repo: str, number: int) -> dict:
    """Полное состояние issue ИЛИ PR по общему issues-эндпоинту (PR доступен
    через него же) — не только `state`, но и `closed_at`/`closed_by` (полный
    single-issue ответ несёт оба поля, список-эндпоинт — нет), нужные для
    следа снятия метки (`removal_comment_text`, #1157). Пустой словарь —
    честный признак «ответа не было» (не путать с открытым issue)."""
    data = pulse_guard.gh(f"repos/{repo}/issues/{number}")
    return data if isinstance(data, dict) else {}


def remove_label(repo: str, number: int) -> None:
    # label_query_value — то же место кодирования, что уже применяет
    # waiting_owner_guard.remove_label (issue #938: сырое двоеточие в пути
    # ломает gh api на плейсхолдерах `:owner`/`:repo`); у `blocked` спецсимволов
    # нет, но кодирование — не второе место правды, а то же самое, что и у
    # значения query выше (open_blocked_issues).
    pulse_guard.gh("-X", "DELETE",
                   f"repos/{repo}/issues/{number}/labels/"
                   f"{review_labels.label_query_value(BLOCKED_LABEL)}")


def stale_blocked_check(repo: str) -> list[str]:
    """Проводка: один живой прогон. Возвращает строки отчёта (пустой список —
    холостой ход, ни одной протухшей блокировки не найдено).

    С #1157: найденная протухшая блокировка (маркер + закрытая цель) больше
    не только эскалируется — метка снимается сама, след оставляется
    комментарием (`removal_comment_text`). Провал самого снятия (сеть/права)
    — единственный путь, оставшийся у эскалации `escalate()` в этом модуле."""
    issues = open_blocked_issues(repo)
    for issue in issues:
        issue["comments_text"] = fetch_comments_text(repo, issue["number"])

    referenced: set[int] = set()
    for issue in issues:
        texts = [issue.get("body") or ""] + issue["comments_text"]
        referenced.update(stale_marker_targets(issue["number"], texts))

    states = {number: issue_state(repo, number) for number in referenced}
    closed_numbers = {number for number, data in states.items() if data.get("state") == "closed"}
    violations = find_stale_blocked(issues, closed_numbers)
    if not violations:
        return [f"💗 blocked: протухших меток не найдено ({len(issues)} issue с меткой blocked проверено)"]

    by_number = {issue["number"]: issue for issue in issues}
    lines = []
    for violation in violations:
        number = violation["number"]
        try:
            remove_label(repo, number)
        except RuntimeError as error:
            # Снятие не удалось (сеть/права) — тот же класс, что провал
            # remove_label в waiting_owner_guard.py: не тонем молча, красная
            # строка отчёта плюс эскалация тем же каналом, что раньше несла
            # само протухание (метка так и осталась висеть без газа). Дедуп
            # эскалации на эпизод (already_escalated) сохранён здесь ЖЕ:
            # повторяющийся сетевой отказ не должен слать Telegram каждые
            # 15 минут (тот же приём, что был у всего модуля до #1157).
            print(f"::warning::метка blocked не снята с #{number}: {error}", file=sys.stderr)
            issue = by_number[number]
            texts = [issue.get("body") or ""] + issue["comments_text"]
            target = escalation_target(violation)
            if already_escalated(texts, target):
                lines.append(f"🔇 {violation_text(violation)} — метка НЕ снята "
                             f"автоматически ({error}); уже эскалировано в этом эпизоде")
            else:
                delivered = escalate(repo, number, escalation_text(violation))
                lines.append(f"🚨 {violation_text(violation)} — метка НЕ снята "
                             f"автоматически ({error}); эскалировано (сигнал: {delivered})")
            continue
        try:
            pulse_guard.post_issue_comment(repo, number, removal_comment_text(violation, states))
        except RuntimeError as error:
            print(f"::warning::след снятия не оставлен в #{number}: {error}", file=sys.stderr)
        lines.append(f"✅ метка `blocked` снята автоматически — {violation_text(violation)}")
    return lines


def main() -> int:
    repo = os.environ.get("GITHUB_REPOSITORY", "mytab0r/edge-harness")
    lines = stale_blocked_check(repo)
    for line in lines:
        print(line)
    # 💗 — холостой ход, ✅ — протухшая блокировка найдена и УЖЕ ПОЧИНЕНА этим
    # же прогоном (метка снята, след оставлен, #1157) — оба зелёные. 🚨 — само
    # снятие не удалось (сеть/права) — красный шаг остаётся видимым при
    # continue-on-error (находка ревью #333/#336), это и есть газ для отказа
    # газа: провал автоматики виден так же, как раньше было видно протухание.
    return 0 if all(line.startswith("💗") or line.startswith("✅") for line in lines) else 1


if __name__ == "__main__":
    sys.exit(main())
