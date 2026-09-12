#!/usr/bin/env python3
"""Алерты Dependabot → задача пула (сирота B, аудит владельца 2026-09-11).

Факт (замер 2026-09-11): `git grep "dependabot/alerts\\|code-scanning/alerts"`
по scripts/ — пусто, ни одной строки кода. GitHub копит алерты сам (сейчас
1 открытый, `high`, пакет `sharp`, с 2026-09-08), но в конвейер этот канал
не заведён — тот же класс, что уже закрыт для провалов CI (#477,
`pulse_guard.failure_watch`), только для другого источника фактов.

## Права (проверено фактом, не предположено)

`gh api repos/mytab0r/edge-harness/dependabot/alerts --jq 'length'` под личным
токеном владельца вернул `1` — эндпоинт читается. Вопрос был другой: читает
ли его `GITHUB_TOKEN` воркфлоу (не личный PAT). Ответ — да, при явном
разрешении `permissions: vulnerability-alerts: read` в workflow:
GitHub задокументировал отдельное право `vulnerability-alerts` именно для
`GITHUB_TOKEN` (`vulnerability-alerts: read` — «Read Dependabot alerts»,
raw.githubusercontent.com/github/docs/main/data/reusables/actions/
github-token-scope-descriptions.md, снято 2026-09-11), включено флагом
`vulnerability-alerts-permission` со значением `fpt: '*'` (github.com,
все планы — тот план, на котором живёт этот репозиторий,
raw.githubusercontent.com/github/docs/main/data/features/
vulnerability-alerts-permission.yml). Старое ограничение «Dependabot и
secret scanning алерты не читаются `security-events`, нужен GitHub App или
PAT» относится к permission `security-events` (код-сканирование), не к
отдельному `vulnerability-alerts`, который появился позже именно для этого
случая. `.github/workflows/dependabot-alert-watch.yml` объявляет это право
явно на уровне job. Живого замера права под `GITHUB_TOKEN` НЕТ (честно, не
подтверждено): `workflow_dispatch` с ветки PR GitHub не даёт, а прогон с
default-ветки возможен только ПОСЛЕ мержа — см. пост-мерж проверку в теле
PR #964. Если предположение неверно, `gh api .../dependabot/alerts` отвечает
403 — `open_dependabot_alerts` НЕ ловит эту ошибку (см. докстринг
`dependabot_alert_watch` ниже), она уходит до `main()`, который красит сам
прогон workflow ненулевым кодом возврата: 403 не может стать тихим ⚠️ в
зелёном step summary (находка ревью PR #964, критик, блокер 3 — до этой
правки `main()` возвращал `0` всегда, независимо от исхода).

## Устройство (тот же скелет, что pulse_guard.failure_watch, #477)

  - Дедуп — по номеру алерта (`alert["number"]`, уникален и не переиспользуется
    GitHub на репозиторий), не по тексту, в ДВА слоя над одним уже прочитанным
    списком задач (тот же приём, что failure_watch #610): маркер
    `ALERT_FINGERPRINT_MARKER` в теле заведённой задачи И номер из
    детерминированного заголовка `alert_task_title` (`tracked_alert_numbers`
    читает оба). Одна задача — один алерт, повторный прогон не плодит вторую
    (`dependabot_created_since`/`already_tracked`); потеря HTML-комментария
    правкой тела вторую задачу не заводит.
  - Суточный потолок — `DEPENDABOT_WATCH_DAILY_CAP` новых задач/сутки (по
    факту СОЗДАНИЯ, `state=all`, тот же приём, что `ci_failure_created_since`)
    — массовый bump зависимостей не заливает пул: превышение не тонет
    молча — эскалация (комментарий в WATCHDOG_ISSUE #120 + Telegram,
    `pulse_guard.escalate`, второй канал не заводится) РОВНО один раз на
    календарный день (маркер `DEPENDABOT_WATCH_CAP_MARKER`), даже если в
    одном пульсе потолком отсечено сразу несколько алертов (эскалация — вне
    цикла по алертам, тот же приём, что `cap_skipped_this_pulse` у
    `pulse_guard.failure_watch`, текст называет все отсечённые номера).
  - Факт в тексте задачи — пакет, severity, manifest-путь, версия-фикс,
    которую Dependabot уже предлагает (`security_vulnerability.
    first_patched_version`) — не гипотеза.
  - Газ (закрытие) — каждый пульс перечитывает уже заведённые задачи
    `DEPENDABOT_ALERT_LABEL`; если алерт, на который она ссылается, больше
    не `open` (пофикшен/задизмиссен/устарел), задача закрывается сама с
    комментарием, называющим новое состояние алерта.

Прод-форма фикстур тестов — дословный снимок живого открытого алерта
репозитория (`gh api repos/mytab0r/edge-harness/dependabot/alerts`,
2026-09-11, пакет sharp/GHSA-rgj7-g3m4-5g8c), не пересказ структуры из
документации.

Запуск: python -m pytest scripts/orchestra/test_dependabot_alert_watch.py -q
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent.parent / "lib" / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import os
import re
import sys
from datetime import datetime, timezone

from pulse_guard import WATCHDOG_ISSUE, escalate, gh, issue_marker_times, parse_time

_LIB = Path(__file__).resolve().parents[1] / "lib"
_PI_SPEC = importlib.util.spec_from_file_location("pool_issue", _LIB / "pool_issue.py")
pool_issue = importlib.util.module_from_spec(_PI_SPEC)
_PI_SPEC.loader.exec_module(pool_issue)  # type: ignore[union-attr]

DEPENDABOT_ALERT_LABEL = "dependabot-alert"

# Потолок новых задач/сутки — тот же порядок величины, что
# FAILURE_WATCH_DAILY_CAP (pulse_guard.py): 5 — достаточно для рабочего
# темпа алертов на этот репозиторий (1 открытый на 2026-09-11), не
# выведено из истории (её ещё нет) — честно приблизительно, поднять
# руками, если объём окажется другим.
DEPENDABOT_WATCH_DAILY_CAP = 5

DEPENDABOT_WATCH_CAP_MARKER = "[dependabot-alert-watch: потолок исчерпан"
ALERT_FINGERPRINT_MARKER = "<!-- dependabot-alert-number: "

# Второй слой дедупа — тот же приём, что точное совпадение заголовка у
# pulse_guard.failure_watch (#610): заголовок alert_task_title детерминирован
# по номеру алерта, поэтому номер можно прочитать и из заголовка задачи, чьё
# тело потеряло HTML-комментарий (правка тела — не повод заводить вторую
# задачу на тот же алерт; находка ревью PR #964). Синхронность с фактическим
# шаблоном держит тест, кормящий регулярку результатом самой alert_task_title.
ALERT_TITLE_RE = re.compile(r"^Dependabot alert #(\d+): ")


def open_dependabot_alerts(repo: str) -> list:
    """Открытые алерты Dependabot, постранично (класс #308 — сырая первая
    страница молча теряет хвост)."""
    page = 1
    alerts: list = []
    while True:
        chunk = gh(f"repos/{repo}/dependabot/alerts?state=open&per_page=100&page={page}") or []
        if not isinstance(chunk, list) or not chunk:
            break
        alerts.extend(chunk)
        if len(chunk) < 100:
            break
        page += 1
    return alerts


def open_dependabot_task_issues(repo: str) -> list:
    """Открытые задачи с меткой DEPENDABOT_ALERT_LABEL, постранично (тот же
    класс #308, что open_ci_failure_issues в pulse_guard.py)."""
    page = 1
    issues: list = []
    while True:
        chunk = gh(
            f"repos/{repo}/issues?state=open&labels={DEPENDABOT_ALERT_LABEL}"
            f"&per_page=100&page={page}"
        ) or []
        if not isinstance(chunk, list) or not chunk:
            break
        issues.extend(chunk)
        if len(chunk) < 100:
            break
        page += 1
    return issues


def _alert_number_from_body(body: str):
    """Номер алерта из маркера в теле, либо None (маркера нет/не число)."""
    idx = body.find(ALERT_FINGERPRINT_MARKER)
    if idx == -1:
        return None
    rest = body[idx + len(ALERT_FINGERPRINT_MARKER):]
    num_str = rest.split(" ", 1)[0].split("-->", 1)[0].strip()
    try:
        return int(num_str)
    except ValueError:
        return None


def _alert_number_from_title(title: str):
    """Номер алерта из детерминированного заголовка alert_task_title, либо
    None (второй слой дедупа, #610)."""
    match = ALERT_TITLE_RE.match(title or "")
    return int(match.group(1)) if match else None


def tracked_alert_numbers(issues: list) -> dict:
    """{номер алерта: issue} — два слоя дедупа над ОДНИМ уже прочитанным
    списком задач (тот же приём, что у failure_watch #610: отпечаток в теле
    ПЛЮС номер из детерминированного заголовка `alert_task_title`). Маркер
    в теле единственным слоем быть не должен: правка тела, потерявшая
    HTML-комментарий, иначе тихо завела бы вторую задачу на тот же алерт
    (находка ревью PR #964)."""
    found: dict = {}
    for issue in issues:
        number = _alert_number_from_body(issue.get("body") or "")
        if number is None:
            number = _alert_number_from_title(issue.get("title") or "")
        if number is not None:
            found[number] = issue
    return found


def dependabot_created_since(repo: str, since: datetime) -> int:
    """Сколько задач DEPENDABOT_ALERT_LABEL заведено не раньше `since` —
    потолок считается по факту создания (`state=all`), не по текущей
    открытости (тот же приём, что ci_failure_created_since)."""
    page = 1
    count = 0
    while True:
        chunk = gh(
            f"repos/{repo}/issues?state=all&labels={DEPENDABOT_ALERT_LABEL}"
            f"&per_page=100&page={page}"
        ) or []
        if not isinstance(chunk, list) or not chunk:
            break
        count += sum(
            1 for issue in chunk
            if "pull_request" not in issue and parse_time(issue["created_at"]) >= since
        )
        if len(chunk) < 100:
            break
        page += 1
    return count


def cap_exhausted(created_today: int, cap: int = DEPENDABOT_WATCH_DAILY_CAP) -> bool:
    return created_today >= cap


def remediation_text(alert: dict) -> str:
    vuln = alert.get("security_vulnerability") or {}
    patched = (vuln.get("first_patched_version") or {}).get("identifier")
    if patched:
        return f"обновить `{_package_name(alert)}` до версии {patched} (или выше)"
    return "патч ещё не выпущен — Dependabot версию не предлагает, ждать фикса или менять зависимость"


def _package_name(alert: dict) -> str:
    return ((alert.get("dependency") or {}).get("package") or {}).get("name", "?")


def _severity(alert: dict) -> str:
    return (alert.get("security_advisory") or {}).get("severity", "?")


def alert_task_title(alert: dict) -> str:
    return f"Dependabot alert #{alert['number']}: {_package_name(alert)} ({_severity(alert)})"


def alert_task_body(alert: dict) -> str:
    dep = alert.get("dependency") or {}
    manifest = dep.get("manifest_path", "?")
    summary = (alert.get("security_advisory") or {}).get("summary", "")
    html_url = alert.get("html_url", "")
    return (
        f"## Цель\n"
        f"Пока алерт безопасности Dependabot #{alert['number']} по пакету "
        f"`{_package_name(alert)}` в состоянии `open` — эта задача ждёт "
        "(находка ai-review PR #964: тело не должно лгать в настоящем "
        "времени, что алерт уже пофикшен, — на момент создания задачи он "
        "ещё open). Как только он станет `fixed`/`dismissed`/устареет — "
        "задача закроется сама.\n\n"
        "## Критерий готовности\n"
        f"Алерт #{alert['number']} не `open` — эта задача закрывается "
        "автоматически тем же наблюдателем (dependabot_alert_watch.py), "
        "как только это произойдёт; закрывать руками не нужно.\n\n"
        "## Факт\n"
        f"Пакет `{_package_name(alert)}` ({manifest}), severity **{_severity(alert)}**: {summary}\n"
        f"Чем лечится: {remediation_text(alert)}.\n"
        f"{html_url}\n\n"
        f"{ALERT_FINGERPRINT_MARKER}{alert['number']} -->\n"
    )


def close_resolved_alert_tasks(repo: str, tracked: dict, open_numbers: set) -> tuple:
    """Газ: задача, чей алерт больше не среди open_numbers, закрывается с
    комментарием, называющим новое состояние (перечитан РЕАЛЬНЫЙ алерт по
    номеру — не просто «исчез из списка open», а конкретное состояние:
    fixed/dismissed/auto_dismissed).

    Порядок — СНАЧАЛА закрытие, потом комментарий (находка ревью PR #964):
    обратный порядок на упавшем закрытии оставлял ОТКРЫТУЮ задачу с лгущим
    комментарием «закрываю задачу», и каждый следующий пульс постил бы его
    заново. Закрытая задача уходит из open-списка — повторов нет; упавший
    комментарий после успешного закрытия — мягкое наблюдение, газ уже
    сработал. Расхождение источников («список сказал не open, точечное
    чтение говорит open») закрытию не подлежит — ⚠️ и следующий пульс
    (тот же класс «два источника разошлись», что diff_source_mismatch
    #687; находка ревью PR #964)."""
    observations: list = []
    actions: list = []
    for number, issue in tracked.items():
        if number in open_numbers:
            continue
        try:
            alert = gh(f"repos/{repo}/dependabot/alerts/{number}")
        except RuntimeError as error:
            observations.append(f"⚠️ dependabot-alert-watch: алерт #{number} не перечитан ({error})")
            continue
        state = (alert or {}).get("state", "?")
        if state == "open":
            observations.append(
                f"⚠️ dependabot-alert-watch: алерт #{number} исчез из списка открытых, "
                f"но точечное чтение отвечает `open` — расхождение источников, "
                f"задача #{issue['number']} остаётся открытой до следующего пульса")
            continue
        try:
            gh("-X", "PATCH", f"repos/{repo}/issues/{issue['number']}", "-f", "state=closed")
        except RuntimeError as error:
            observations.append(
                f"⚠️ dependabot-alert-watch: закрытие #{issue['number']} не удалось ({error}) — "
                "комментарий не постился, задача остаётся на следующий пульс")
            continue
        try:
            gh("-X", "POST", f"repos/{repo}/issues/{issue['number']}/comments",
               "-f", "body=" + f"Алерт Dependabot #{number} больше не `open` "
               f"(состояние: `{state}`) — задача закрыта.")
        except RuntimeError as error:
            observations.append(
                f"⚠️ dependabot-alert-watch: #{issue['number']} закрыта, но комментарий "
                f"о состоянии алерта #{number} не постился ({error})")
            continue
        actions.append(
            f"✅ dependabot-alert-watch: закрыта #{issue['number']} (алерт #{number} → {state})")
    return observations, actions


def dependabot_alert_watch(repo: str, now: datetime) -> tuple:
    """Один пульс: закрыть решённые, завести новые (в пределах потолка,
    эскалируя исчерпание раз в календарный день).

    `open_dependabot_alerts(repo)` НЕ оборачивается try/except здесь
    (находка ревью PR #964, критик, блокер 3): это ровно тот запрос, для
    которого заведено право `vulnerability-alerts: read` — единственный
    живой способ узнать, что право не работает (403) или транспорт мёртв,
    это дать вызову упасть. RuntimeError уходит наверх, main() красит
    прогон ненулевым кодом — без этого 403 стал бы тихим ⚠️ в зелёном
    step summary НАВСЕГДА (workflow_dispatch с ветки PR недоступен, прогон
    на default-ветке возможен только ПОСЛЕ мержа — постфактум замер живым
    правом до мержа никто не проводил, см. тело PR #964/пост-мерж проверку).
    Постмортем #255 (AGENTS.md): «конвейер простоял сутки при сплошь
    зелёных прогонах» — тот же класс."""
    observations: list = []
    actions: list = []

    alerts = open_dependabot_alerts(repo)
    open_numbers = {a["number"] for a in alerts if isinstance(a.get("number"), int)}

    try:
        pool_issues = open_dependabot_task_issues(repo)
    except RuntimeError as error:
        # Находка живого AI-ревью PR #964 (rework, head bcaf99c4): подмена
        # сбоя чтения пустым списком (`pool_issues = []`) обнуляла дедуп
        # (`tracked`), и цикл заведения ниже продолжал работать как будто
        # ни один алерт ещё не отслежен — транзиентный 503 на списке задач
        # заводил дубль уже существующей задачи. Тот же приём, что
        # pulse_guard.failure_watch на сбое чтения списка прогонов workflow
        # (see `except RuntimeError: ... continue`): не знаем состав
        # tracked — не гадаем, что он пуст, пропускаем весь пульс заведения/
        # закрытия целиком ("Алерт не гадает", AGENTS.md).
        observations.append(
            f"⚠️ dependabot-alert-watch: список задач {DEPENDABOT_ALERT_LABEL} не прочитан ({error}) — "
            "дедуп недоступен, этот пульс не заводит и не закрывает задачи")
        return observations, actions
    tracked = tracked_alert_numbers(pool_issues)

    close_obs, close_actions = close_resolved_alert_tasks(repo, tracked, open_numbers)
    observations += close_obs
    actions += close_actions

    created_today = None
    counter_failed = False
    # Отсечённые потолком в ЭТОМ пульсе — эскалация #120+Telegram уходит
    # РОВНО один раз ПОСЛЕ цикла (тот же приём, что pulse_guard.failure_watch:
    # cap_skipped_this_pulse), а не на каждый отсечённый алерт внутри цикла —
    # иначе шесть алертов, отсечённых одним и тем же исчерпанным потолком в
    # одном пульсе, дали бы шесть Telegram-сообщений вместо одного.
    cap_skipped: list = []
    for alert in alerts:
        number = alert.get("number")
        if not isinstance(number, int) or number in tracked:
            continue
        if counter_failed:
            # Счётчик этого пульса не читается (ниже) — не знаем, исчерпан ли
            # потолок, поэтому не заводим ничего до следующего пульса. Нулевой
            # подменой это не лечится: нечитаемый предохранитель, действующий
            # как «потолка нет», заводит задачи РОВНО тогда, когда ограничитель
            # слеп (находка живого AI-ревью PR #964, head 5c9bc3d; тот же
            # приём, что failure_watch на нечитаемом счётчике — continue).
            observations.append(
                f"⏭️ dependabot-alert-watch: алерт #{number} не заведён — счётчик "
                "потолка не прочитан, задачи в этом пульсе не заводятся")
            continue
        if created_today is None:
            since = now.replace(hour=0, minute=0, second=0, microsecond=0)
            try:
                created_today = dependabot_created_since(repo, since)
            except RuntimeError as error:
                counter_failed = True
                observations.append(
                    f"⚠️ dependabot-alert-watch: счётчик потолка не прочитан ({error}) — "
                    "не знаю, исчерпан ли суточный потолок, алерт "
                    f"#{number} и следующие в этом пульсе не заводятся")
                continue
        if cap_exhausted(created_today):
            cap_skipped.append(number)
            observations.append(
                f"⏭️ dependabot-alert-watch: алерт #{number} отсечён потолком "
                f"({created_today}/{DEPENDABOT_WATCH_DAILY_CAP})")
            continue
        title = alert_task_title(alert)
        body = alert_task_body(alert)
        try:
            created = pool_issue.create_pool_issue(gh, repo, title, body, ["task", DEPENDABOT_ALERT_LABEL])
        except RuntimeError as error:
            observations.append(f"⚠️ dependabot-alert-watch: алерт #{number} не заведён ({error})")
            continue
        created_today += 1
        actions.append(f"📋 dependabot-alert-watch: алерт #{number} → задача #{created['number']}")

    if cap_skipped:
        marker = f"{DEPENDABOT_WATCH_CAP_MARKER} {now.date().isoformat()}]"
        try:
            already = bool(issue_marker_times(repo, WATCHDOG_ISSUE, marker))
        except RuntimeError as error:
            observations.append(
                f"⚠️ dependabot-alert-watch: маркеры #{WATCHDOG_ISSUE} не прочитаны ({error})")
            already = True  # не спамим при сбое чтения
        if not already:
            escalate(
                repo, WATCHDOG_ISSUE,
                f"🚨 edge-harness: {marker}\n"
                f"Суточный потолок автозаведения {DEPENDABOT_ALERT_LABEL} задач "
                f"({created_today}/{DEPENDABOT_WATCH_DAILY_CAP}) исчерпан — алерты "
                f"{sorted(cap_skipped)} НЕ завели задачу автоматически, нужен человек.\n\n"
                "Что дальше: посмотреть репозиторий Security → Dependabot, "
                "завести задачу вручную (scripts/gh/issue-create) или поднять "
                "DEPENDABOT_WATCH_DAILY_CAP, если объём временный."
            )

    if not observations and not actions:
        observations.append("dependabot-alert-watch: открытых алертов нет")
    return observations, actions


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    now = datetime.now(timezone.utc)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    try:
        observations, actions = dependabot_alert_watch(repo, now)
    except RuntimeError as error:
        text = (
            f"🚨 dependabot-alert-watch: список алертов не прочитан ({error}) — "
            "право vulnerability-alerts: read отсутствует или транспорт "
            "сломан, прогон красный (fail loud, не тихий пропуск, см. "
            "докстринг dependabot_alert_watch)."
        )
        print(text, file=sys.stderr)
        if summary_path:
            with open(summary_path, "a", encoding="utf-8") as file:
                file.write("## dependabot-alert-watch\n\n" + text + "\n")
        return 1
    lines = ["## dependabot-alert-watch", ""] + observations + actions
    text = "\n".join(lines) + "\n"
    print(text)
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as file:
            file.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
