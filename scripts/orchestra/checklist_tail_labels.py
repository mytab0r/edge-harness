#!/usr/bin/env python3
"""Хвост чеклиста ревью наследует область родившего PR — на деле не наследует
(сирота A, аудит владельца 2026-09-11).

Факт (замер владельца 2026-09-11, начало аудита): за 5 дней `after_merge`
(scripts/orchestra/scheduler.py) завела 46 задач «Хвост чеклиста ревью PR
#N», закрыто — 0. Число растёт с каждым слитым PR, у которого остался
незакрытый пункт чеклиста — к моменту разбора этого модуля (тот же день,
несколько часов спустя) было уже 48 открытых, к моменту исполнения
backfill'а против живого репозитория (см. тело PR #964) — снова другое
число: снимок, не константа. Прочитано 7 из 46 целиком: содержимое настоящее (конкретные file:line,
проверяемые находки), не мусор и не дубли — так что дело не в ценности, а
в приоритете выбора. Тело каждой такой задачи (`review_checklist.
tail_issue_body`) прямо заявляет:

    «Область и приоритет — как у породившего PR»

но labels, с которыми `create_pool_issue` реально заводит issue
(scheduler.py::after_merge; в main на момент этого PR — строки 2222-2226),
— только `["task"]`. Обещание не выполняется:
`scripts/lib/free_task.py::issue_priority_key` знает ровно два рычага
приоритета — метку `area:process` (уровень 1) и число открытых задач,
которые issue блокирует (уровень 2, граф `blockedBy`); без обоих хвост
конкурирует по номеру issue среди ~180 обычных свободных задач пула (замер
2026-09-11: 260 свободных всего, из них 80 — `area:process`, которые
`free_task()` выбирает раньше ЛЮБОГО обычного кандидата) — отсюда и нулевое
закрытие: не потому что находки бесполезны, а потому что до них никогда не
доходит очередь.

Мутирующий scheduler.py (после_merge) сейчас занят параллельными PR
(#950/#956/#942 и другими, см. README задачи) — здесь чинится ТОЛЬКО
доступная половина: докрутить labels уже заведённых задач мехАнически —
не оценкой машиной «эта находка ценна», а буквальным выполнением уже
написанного обещания «как у породившего PR» — скопировать `area:*`-метки
РОДИТЕЛЬСКОЙ задачи (той, чью ветку называла agent-ветка слитого PR).
Никакого нового суждения о ценности здесь нет: это тот же самый выбор
`area:process`, который уже стоит на родительской задаче — то есть кто-то
(агент/владелец) его уже сделал, этот модуль только переносит факт, не
изобретает его (см. review_checklist.py: «Оценка ценности/приоритета
находок в чеклисте — решение человека, машины здесь нет» — актуально для
СОЗДАНИЯ хвоста; перенос уже принятого решения родителя — не создание
нового суждения).

Точное place-to-fix в scheduler.py (для того, у кого нет конфликта с
параллельными PR): scheduler.py::after_merge, вызов `create_pool_issue`
для хвоста (в main на момент этого PR — строки 2222-2226):

    created = pool_issue.create_pool_issue(
        gh, repo, tail_title,
        review_checklist.tail_issue_body(repo, number, unresolved),
        ["task"],
    )

заменить последний аргумент на `["task", *checklist_tail_labels.
inheritable_labels_for_pr(repo, number, gh)]` — тогда наследование
происходит в момент создания, и периодический прогон этого файла
становится страховкой на случай сбоя сети в тот момент, а не единственным
путём.

Пока это не сделано, `checklist_tail_labels(repo)` — отдельный периодический
потребитель (workflow `.github/workflows/checklist-tail-triage.yml`, не
внутри orchestra.yml — тот же приём независимости, что и
dependabot_alert_watch.py рядом): читает открытые задачи с заголовком
`tail_issue_title`, резолвит PR → родительскую задачу (`task_ref.
resolve_pr_task`, тот же единственный источник — имя agent-ветки, что и
везде в репозитории) → её `area:*`-метки → добавляет недостающие сюда.
Идемпотентность — маркер `INHERITED_MARKER_LABEL`: разобранный хвост
(даже если у родителя не нашлось ни одной `area:*`) помечается и больше не
трогается — обычный пульс не гоняет сеть повторно ради уже решённых
хвостов.

Запуск: python -m pytest scripts/orchestra/test_checklist_tail_labels.py -q
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

from pulse_guard import gh

# Импорт по файлу (паттерн claim_task/review_labels/scheduler.py) — скрипты
# запускаются как файлы, не как пакет.
_LIB = Path(__file__).resolve().parents[1] / "lib"

_RL_SPEC = importlib.util.spec_from_file_location("review_labels", _LIB / "review_labels.py")
review_labels = importlib.util.module_from_spec(_RL_SPEC)
_RL_SPEC.loader.exec_module(review_labels)  # type: ignore[union-attr]

_TR_SPEC = importlib.util.spec_from_file_location("task_ref", _LIB / "task_ref.py")
task_ref = importlib.util.module_from_spec(_TR_SPEC)
_TR_SPEC.loader.exec_module(task_ref)  # type: ignore[union-attr]

TASK_LABEL = "task"

# Зеркало формата scripts/lib/review_checklist.py::tail_issue_title —
# синхронность с фактическим форматом держит
# test_tail_title_re_matches_real_tail_issue_title (кормит регулярку
# результатом самой функции, не пересказом), не копия строки-литерала
# наугад.
TAIL_TITLE_RE = re.compile(r"^Хвост чеклиста ревью PR #(\d+)$")

# Маркер «этот хвост уже разобран» — ставится независимо от результата
# (нашлась ли area:*-метка у родителя), чтобы обычный пульс не гонял сеть
# повторно ради уже решённых хвостов (родитель без area:* — тоже решённый
# случай, не забытый).
INHERITED_MARKER_LABEL = "checklist-tail:inherited"

# Метки родителя, которые НЕ являются «областью» в смысле обещания «Область
# и приоритет — как у породившего PR»: временные состояния конвейера
# (ревью-гейты, aренда, служебные пометки), не классификация задачи.
# Наследуются только area:*, которых здесь нет.
NON_INHERITABLE_LABELS = {
    TASK_LABEL,
    INHERITED_MARKER_LABEL,
    "stale-unclaimed",
    "blocked",
    "waiting:owner",
    "auto-detected",
    "conflict",
    "white-spot",
    "review:ok",
    "review:changes-requested",
    "review:large",
    "review:large-ok",
    "ai:ok",
    "ai:changes-requested",
    "ai:failed",
}


def has_label(issue: dict, label: str) -> bool:
    return label in {entry["name"] for entry in (issue.get("labels") or [])}


def inheritable_labels(parent_task_labels: list) -> set:
    """area:*-метки родительской задачи, за вычетом NON_INHERITABLE_LABELS
    (там их не бывает по построению — фильтр на случай, если конвенция
    когда-нибудь заведёт area:-префикс для служебной пометки)."""
    names = {entry["name"] for entry in (parent_task_labels or [])}
    return {name for name in names if name.startswith("area:")} - NON_INHERITABLE_LABELS


def open_tail_issues(repo: str) -> list:
    """Открытые задачи-хвосты чеклиста ревью — issues с меткой task, чей
    заголовок матчит TAIL_TITLE_RE. Тот же обход, что open_task_issues в
    scheduler.py (класс #308 — обход постранично, не сырая первая
    страница), не импортирован напрямую из scheduler.py, чтобы этот модуль
    не тянул scheduler.py как зависимость (независимость наблюдателя, тот
    же приём, что stall_detector.py/dependabot_alert_watch.py)."""
    issues = review_labels.list_pages(
        f"repos/{repo}/issues?state=open&labels={review_labels.label_query_value(TASK_LABEL)}&per_page=100", gh)
    return [
        issue for issue in issues
        if "pull_request" not in issue and TAIL_TITLE_RE.match(issue.get("title") or "")
    ]


def resolve_parent_task(repo: str, pr_number: int):
    """PR #pr_number → номер задачи из пула, тем же единственным источником,
    что везде в репозитории (имя agent-ветки, `task_ref.resolve_pr_task`) —
    тело PR не читается. `None` — PR прочитан, но честно не резолвится
    (ветка не agent-формы). RuntimeError чтения PR (502/503/лимит) НЕ
    ловится здесь и уходит вызывающему (находка живого AI-ревью PR #964,
    rework, head bcaf99c4): раньше оба случая — «транспорт сломан» и
    «ветка не agent-формы» — схлопывались в один и тот же `None`, и
    `apply_inherited_labels` ставил `INHERITED_MARKER_LABEL` навсегда по
    транзиентному сбою, а строка отчёта лгала «не agent-ветка», хотя код
    этого не знал ("Алерт не гадает", AGENTS.md). `apply_inherited_labels`
    не оборачивает этот вызов, RuntimeError долетает до `checklist_tail_labels`,
    которая уже репортит ⚠️ по конкретному хвосту БЕЗ маркера — следующий
    пульс попробует снова."""
    pull = gh(f"repos/{repo}/pulls/{pr_number}")
    if not pull:
        return None
    return task_ref.resolve_pr_task(pull)


def apply_inherited_labels(repo: str, tail_issue: dict):
    """Один хвост: резолвит PR → родителя → его area:*-метки, добавляет
    недостающие плюс маркер INHERITED_MARKER_LABEL одним вызовом. Строка
    для отчёта, либо None — хвост уже маркирован, трогать нечего."""
    if has_label(tail_issue, INHERITED_MARKER_LABEL):
        return None
    match = TAIL_TITLE_RE.match(tail_issue.get("title") or "")
    if not match:
        return None
    pr_number = int(match.group(1))
    number = tail_issue["number"]
    task_number = resolve_parent_task(repo, pr_number)
    if task_number is None:
        gh("-X", "POST", f"repos/{repo}/issues/{number}/labels",
           "-f", f"labels[]={INHERITED_MARKER_LABEL}")
        return (f"ℹ️ checklist-tail-labels: #{number} — PR #{pr_number} не резолвится "
                "в задачу пула (не agent-ветка), область не наследуется")
    try:
        parent = gh(f"repos/{repo}/issues/{task_number}")
    except RuntimeError as error:
        return f"⚠️ checklist-tail-labels: #{number} — задача #{task_number} не прочитана ({error})"
    to_inherit = inheritable_labels((parent or {}).get("labels") or [])
    already = {entry["name"] for entry in (tail_issue.get("labels") or [])}
    missing = sorted(to_inherit - already)
    args = ["-X", "POST", f"repos/{repo}/issues/{number}/labels"]
    for label in [*missing, INHERITED_MARKER_LABEL]:
        args += ["-f", f"labels[]={label}"]
    gh(*args)
    if missing:
        return (f"🏷️ checklist-tail-labels: #{number} унаследовал {missing} "
                f"у задачи #{task_number} (PR #{pr_number})")
    if to_inherit:
        # Различать пустой to_inherit и пустой missing обязан сам отчёт
        # (находка ревью PR #964): «нет area:*-меток» при уже стоящей области
        # — ложный факт, читатель уходит с неверной картиной родителя
        # («Алерт не гадает», AGENTS.md).
        return (f"ℹ️ checklist-tail-labels: #{number} — область {sorted(to_inherit)} "
                f"задачи #{task_number} уже стоит на хвосте (PR #{pr_number})")
    return (f"ℹ️ checklist-tail-labels: #{number} — у задачи #{task_number} "
            "нет area:*-меток для наследования")


def checklist_tail_labels(repo: str) -> list:
    """Список открытых хвостов НЕ оборачивается try/except здесь (находка
    ревью PR #964, критик, блокер 3): это единственный запрос, отвечающий
    на вопрос «жив ли механизм вообще» (право issues/pull-requests read у
    GITHUB_TOKEN, транспорт до GitHub API). RuntimeError уходит наверх,
    main() красит прогон ненулевым кодом — 403/сеть здесь не может стать
    тихим ⚠️ в зелёном отчёте (постмортем #255, AGENTS.md: «конвейер простоял
    сутки при сплошь зелёных прогонах»). Сбой ПО ОДНОМУ хвосту
    (apply_inherited_labels — PR/задача этого конкретного хвоста не
    прочитались) остаётся мягким наблюдением: соседние хвосты в том же
    пульсе читаются независимо, единичный сбой не означает смерть всего
    механизма."""
    tails = open_tail_issues(repo)
    lines = []
    for tail in tails:
        try:
            line = apply_inherited_labels(repo, tail)
        except RuntimeError as error:
            line = f"⚠️ checklist-tail-labels: #{tail.get('number')} — сбой ({error})"
        if line:
            lines.append(line)
    if not lines:
        lines.append("checklist-tail-labels: новых неразобранных хвостов не найдено")
    return lines


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    try:
        lines = checklist_tail_labels(repo)
    except RuntimeError as error:
        text = (
            f"🚨 checklist-tail-labels: список хвостов не прочитан ({error}) — "
            "право/транспорт сломаны, прогон красный (fail loud, не тихий "
            "пропуск, см. докстринг checklist_tail_labels)."
        )
        print(text, file=sys.stderr)
        if summary_path:
            with open(summary_path, "a", encoding="utf-8") as file:
                file.write("## checklist-tail-labels\n" + text + "\n")
        return 1
    text = "\n".join(lines) + "\n"
    print(text)
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as file:
            file.write("## checklist-tail-labels\n" + text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
