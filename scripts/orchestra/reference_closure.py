#!/usr/bin/env python3
"""Приёмка по ссылке (#1042): закрывает задачи, которые слитый PR явно
заявляет ЧУЖИМИ — не своей веткой.

## Место в конвейере

`scheduler.py::accept_merged_tasks` (#227) уже закрывает задачу, названную
ИМЕНЕМ ВЕТКИ слитого PR (`task_ref.resolve_pr_task`, #394) — единственный
узкий резолвер «какая задача у ЭТОГО PR». Это НЕ единственный факт «какие
задачи закрыл слитый PR»: измерено на живом пуле (ревизия 2026-09-12,
22 закрытых задачи, два прохода) — 3 из 5 закрытий одного класса («предмет
уже слит, задача осталась открыта») резолвятся ИНАЧЕ: слитый PR прозой
заявляет ЧУЖУЮ задачу как покрытую, а не только собственную:

  - PR #986 (ветка `agent/806-*`) — «закрываю issue #507 этим PR как
    полностью покрытый»;
  - PR #952 (ветка `agent/945-*`) — «закрывает #661, вариант 1»;
  - PR #841 (ветка `agent/840-*`) — «Закрыт класс #786 по всему `scripts/`».

`repo_invariants.py::check_reopened_after_merge` (инвариант 1) уже находит
такие задачи (открыта, без исполнителя, есть слитый PR своей ветки) и
эскалирует в WATCHDOG_ISSUE (#120) — но только для задач, у которых слитый
PR НАЗЫВАЕТ ИХ СВОЕЙ ВЕТКОЙ. Для трёх случаев выше собственная ветка PR
называет ДРУГУЮ задачу (#806/#945/#840) — сам номер #507/#661/#786 в
инварианте 1 не появляется вовсе, потому что резолвер PR→задача (branch)
там тот же самый узкий `resolve_pr_task`. Этот модуль — второй потребитель
того же факта «предмет уже слит», работающий по ВТОРОМУ источнику связи
(`task_ref.also_closes_targets`, строгий маркер в теле, не проза).

## Второй источник — почему не Closes/Fixes/Resolves и не проза

`Closes/Fixes/Resolves #N` запрещены контрактом (#953, `scripts/git/
pr-create`): GitHub закрыл бы задачу НЕМЕДЛЕННО при мерже, до пост-мерж
проверки, которую критерий часто требует (деплой, канарейка, E2E) — тот же
довод, по которому `accept_merged_tasks` вообще не читает ключевые слова.
`extract_task_refs`/`references_task` (любое упоминание `#N`) слишком широки
для НЕОБРАТИМОГО действия (закрытие) — живой урок #90 (два раза потеряла
аренду из-за случайного упоминания номера в чужом теле, docstring
`task_ref.py`).

`also_closes_targets` — третий, строгий источник между этими двумя:
конкретный список русских глагольных форм («закрывает», «закрываю»,
«закрыл(а|о)?», «закрыт(а|о|ы)?») рядом с `#N`. Русские слова НЕ входят в
фиксированный английский список ключевых слов GitHub (close/closes/closed/
fix/fixes/fixed/resolve/resolves/resolved — без перевода на другие языки,
официальное поведение) — автозакрытие при мерже здесь СТРУКТУРНО
невозможно, а не просто запрещено соглашением, как английские директивы.
Открытый корень (`закры[а-я]*`) отклонён экспериментом на живом теле PR
#986: фраза «зависит закрытие #806» (существительное) ложно совпала бы, а
тот же PR прямо пишет, что #806 «не закрывается автоматически этим PR» —
подробности и мутационный тест в `scripts/lib/task_ref.py`/
`test_task_ref.py`.

## Что проверяется машинно (материальная улика)

Как и штатная приёмка (`accept_merged_tasks`), категория улики по составу
файлов слитого PR (`scheduler.classify_acceptance`/`deploy_evidence`/
`script_evidence`/`docs_missing` — переиспользованы, не продублированы,
`scheduler.py` не правится этим модулем, только читается тем же приёмом,
что уже применяет `repo_invariants.py`). Плюс два узких дополнения,
специфичных для ВТОРОГО источника (у него нет собственной ветки, значит нет
и презумпции «эта работа для этой задачи», которую даёт совпадение имени
ветки при обычной приёмке):

  1. `merge_commit_on_main` — коммит слияния ДЕЙСТВИТЕЛЬНО предок main
     (GitHub compare API, `status in ("identical", "behind")`), а не просто
     «где-то был зелёный прогон». Живая дыра, названная владельцем (#925,
     2026-09-12): три «зелёных» прогона deploy цитировались как улика для
     `head_sha`, не являющихся предками main. `deploy_evidence` сам по себе
     сверяет прогон с `merge_commit_sha` PR (см. её докстринг) — это
     дополнение проверяет, что САМ `merge_commit_sha` реально влит в main,
     а не только что он существует в ответе API мержа.
  2. `guard_test_evidence` — если слитый PR добавил гвардию каталога
     (`scripts/ci/guards/*.sh`, #749), проверяет, что файл гвардии и
     называемый ею тестовый файл существуют на main и что тестовый файл
     текстово несёт слово «мутаци» (эвристика: докстринг/тесты этого
     репозитория дословно описывают мутацию, см. любой существующий
     `test_*.py`). Честная граница: это НЕ повторный прогон мутации (снять
     фикс → красный → вернуть → зелёный) — только текстовый признак, что
     доказательство где-то в файле есть. Полноценный повторный прогон
     потребовал бы временного отката кода на диске и повторного pytest —
     дороже, чем оправдано для этой узкой проверки; если этого признака
     мало, эволюция — задача пула, не сделанная этим PR.

Задача, права которой не признаны узнанной ветками выше (нет открытого PR,
нет исполнителя), и не отклонена ими же (эпик, #120, не task-labeled,
конкурирующий PR, коммит не на main, улика fail/pending) — закрывается с
комментарием-уликой; идемпотентность — маркер REFERENCE_CLOSURE_MARKER с
номером ИМЕННО ЭТОГО PR (как ACCEPTANCE_*-маркеры штатной приёмки). Порядок
записи — закрытие ПЕРВЫМ, маркер-комментарий вторым: сбой PATCH оставляет
задачу открытой без маркера (следующий прогон повторяет попытку), а сбой
комментария при уже закрытой задаче — громкая ⚠️-строка отчёта (закрытую
задачу следующий прогон отсекает по state != open) — блокирующая находка
ревью PR #1046, обратный порядок замораживал кандидата навсегда.

## Носитель вызова (класс «потребитель без вызова — мёртвый код»)

Модуль вызывается живым конвейером: шаг «Приёмка по ссылке» в
`.github/workflows/orchestra.yml` (job `orchestra`, continue-on-error:
красный шаг виден, но не гасит зелёный статус job'а, который читает пульс;
GH_TOKEN + TELEGRAM_* — без них escalate вернёт «НЕ доставлен»; гейт квоты
`steps.quota.outputs.skip` — тот же, что у scheduler/repo_invariants, шаг
тяжёлый: пагинированный список слитых PR плюс compare API на кандидата).
До этого PR модуль был мёртвым кодом — вызывался только руками; тест
`test_pipeline_wiring_orchestra_yml_calls_reference_closure` краснеет при
удалении шага (находка AI-ревью PR #1046, закрытие класса).

Код выхода (`exit_code`) держит обещание шага «красный шаг виден» (fail
loud): 1 — ⚠️-строка (операция не удалась) ИЛИ НОВЫЙ 🚨-эпизод (проверка
сломана, эскалация #120 только что отправлена) — тот же класс красит прогон
у штатной приёмки (`accept_hard_failure`), прежняя асимметрия (🚨 возвращает
0) — находка ревью PR #1046 из чеклиста. Повторный 🚨 («уже эскалировано»,
дедуп на эпизод) — 0: алерт уже доставлен, краснить каждый следующий прогон
— тормоз без газа (AGENTS.md); газ здесь — сам дедуп, красный шаг существует
только пока эпизод новый.

## Честная граница — что НЕ передаётся машине этим модулем

  - 2 из 5 живых примеров ревизии (#1021, #967) — ДРУГОЙ класс: их слитый
    PR — это PR СОБСТВЕННОЙ ветки (own-task, `resolve_pr_task` уже находит
    его), но приёмка когда-то поставила `ACCEPTANCE_PARTIAL_MARKER` (тело
    PR несёт дисклеймер о смежной, отдельно заведённой задаче — ложный для
    ЭТОЙ задачи, но `partial_disclaimer` его не отличил). Закрытие требует
    переоценки уже вынесенного терминального вердикта штатной приёмки — это
    правка `scheduler.py::partial_disclaimer` (файл занят параллельными PR
    на момент этой задачи, вне территории #1042). Related-задача заведена
    отдельно (см. proposal этого PR).
  - «Критерий выполнен по существу, а не только код на main» (контрпример
    #389: код слит, ADR написан, но критерий требует ЖИВОЙ директивы
    владельца через инбокс, которой не было), «предмет отменён решением
    владельца» (#901: закрыт мандатом владельца #939, без единого
    закрывающего PR вовсе) и «дубль/соседний случай одного класса»
    (#720/#752, #750/#766 — разные дефекты в одних файлах) — сознательно
    НЕ формализуются здесь, это работа роли (ревизия/человек), а не
    механики. Ни один из этих трёх даже не станет КАНДИДАТОМ этого модуля:
    у #389/#901 нет слитого PR с `also_closes_targets`-маркером на их номер
    (проверено живым прогоном, см. test_reference_closure.py — прод-форма
    их тел), #1021/#967 отфильтрованы тем, что их «доп. задача» совпадает
    с собственной задачей ветки (см. `find_candidates`, исключение own).

Запуск живого прогона: python scripts/orchestra/reference_closure.py
Запуск тестов: python -m pytest scripts/orchestra/test_reference_closure.py -q
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

REPO_ROOT = Path(__file__).resolve().parents[2]
_LIB = REPO_ROOT / "scripts" / "lib"
_ORCH = REPO_ROOT / "scripts" / "orchestra"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


# Импорт по файлу (importlib), не как пакет — паттерн claim_task/review_labels/
# repo_invariants: скрипты этого репозитория запускаются как файлы. scheduler.py
# ТОЛЬКО читается (чистые функции классификации улики) — не правится этим
# модулем, тем же приёмом, что уже применяет repo_invariants.py.
pulse_guard = _load("pulse_guard", _ORCH / "pulse_guard.py")
review_labels = _load("review_labels", _LIB / "review_labels.py")
scheduler = _load("scheduler", _ORCH / "scheduler.py")
task_ref = _load("task_ref", _LIB / "task_ref.py")
claim_task = _load("claim_task", _LIB / "claim_task.py")

gh = pulse_guard.gh
parse_time = pulse_guard.parse_time
escalate = pulse_guard.escalate
issue_marker_times = pulse_guard.issue_marker_times
post_issue_comment = pulse_guard.post_issue_comment
WATCHDOG_ISSUE = pulse_guard.WATCHDOG_ISSUE

TASK_LABEL = scheduler.TASK_LABEL

# ── Маркеры (namespace отдельный от ACCEPTANCE_*, чтобы не путать источник
# закрытия при чтении истории задачи — «приёмка по ветке» и «приёмка по
# ссылке» разные механизмы, ссылаются на разные PR по разным причинам) ──────
REFERENCE_CLOSURE_MARKER = "[приёмка-по-ссылке: закрыто]"
REFERENCE_CLOSURE_ERROR_MARKER = "[приёмка-по-ссылке: сбой]"

# Потолок закрытий за ОДИН прогон (по образцу FAILURE_WATCH_DAILY_CAP=5,
# pulse_guard.py:449, докстринг там же объясняет общий принцип: тормоз без
# потолка заливает пул/закрывает лишнее при аномалии). Здесь риск другой:
# FAILURE_WATCH создаёт лишнюю задачу (обратимо — закрыть дубль), этот модуль
# ЗАКРЫВАЕТ задачу (менее обратимо — правило «закрытая не переоткрывается
# никогда», AGENTS.md, ошибку лечит только НОВАЯ задача, не откат). Число —
# измеренный размер бэклога этого узкого класса за один проход ручной
# ревизии (2026-09-12): 3 задачи. Потолок 5, не 3, — с запасом на рост, но
# заметно ниже, чем «весь пул»: это первая, ещё не проверенная на живом
# объёме версия механизма, и её собственная ошибка (закрыла не то) должна
# быть видна человеку в тот же день, а не после того как утекла партия из
# полусотни. Условие пересмотра — то же, что и у соседних потолков: механизм
# отработал без единой находки ложного закрытия на протяжении заметного
# числа прогонов (не формализовано числом — решение того же порядка, что и
# остальные ручные "газы" в этом репозитории, см. LABELS.md).
REFERENCE_CLOSURE_RUN_CAP = 5

_GUARD_PATH_RE = re.compile(r"^scripts/ci/guards/[\w.-]+\.sh$")
_PYTEST_INVOCATION_RE = re.compile(r"pytest\s+([^\s]+\.py)")
_CHANGE_PATH_RE = re.compile(r"openspec/changes/([A-Za-z0-9][A-Za-z0-9._-]*)")

# Исходы skip — печатаются, не молчат (AGENTS.md, «Алерт не гадает» + «не
# отдавать молчаливый пропуск», прямое требование этого PR).
SKIP_WATCHDOG = "watchdog_issue"
SKIP_NOT_OPEN = "not_open"
SKIP_EPIC = "epic"
SKIP_TASK_LABEL_MISSING = "not_task_labeled"
SKIP_ASSIGNEE = "assignee_present"
SKIP_COMPETING_PR = "competing_open_pr"
SKIP_ALREADY = "already_processed"
SKIP_EVIDENCE_PENDING = "evidence_pending"
SKIP_EVIDENCE_FAIL = "evidence_fail"
SKIP_NOT_ANCESTOR = "merge_commit_not_on_main"
SKIP_CAP = "cap_reached"

_EVIDENCE_SEVERITY = {"ok": 0, "docs": 0, "pending": 1, "fail": 2}


# ══════════════════════════════════════════════════════════════════════════
# Чистые решения (без сети — тестируются на прод-форме фикстур)
# ══════════════════════════════════════════════════════════════════════════


def find_candidates(merged_pulls: list[dict]) -> dict[int, dict]:
    """Task#N → самый свежий слитый PR, чьё тело заявляет #N маркером
    `also_closes_targets`, ИСКЛЮЧАЯ номер, совпадающий с собственной задачей
    ветки этого же PR (`resolve_pr_task`) — это уже штатная территория
    `scheduler.accept_merged_tasks`, второй, независимый путь закрытия того
    же номера тем же PR не заводим (не дублировать маркеры/комментарии на
    одну и ту же пару задача-PR по двум механизмам сразу)."""
    best: dict[int, dict] = {}
    for pull in merged_pulls:
        own_task = task_ref.resolve_pr_task(pull)
        for number in task_ref.also_closes_targets(pull.get("body") or ""):
            if number == own_task:
                continue
            current = best.get(number)
            if current is None or pull["merged_at"] > current["merged_at"]:
                best[number] = pull
    return best


def competing_open_pr(task_number: int, open_pulls: list[dict]) -> int | None:
    """Номер открытого PR, чья СОБСТВЕННАЯ ветка называет эту задачу — узкий
    резолвер (`resolve_pr_task`), не любое упоминание (тот же довод, что и
    в `merged_pr_map`/`reap_stale`: широкая семантика здесь дала бы ложный
    отказ на случайное упоминание номера в чужом PR)."""
    for pull in open_pulls:
        if task_ref.resolve_pr_task(pull) == task_number:
            return pull["number"]
    return None


def guard_paths(filenames: list[str]) -> list[str]:
    return sorted(name for name in filenames if _GUARD_PATH_RE.match(name))


def guard_test_evidence(repo_root: Path, guard_path: str) -> tuple[str, str]:
    """('ok'|'fail'|'pending', детали) для ОДНОЙ гвардии каталога. Честная
    граница (см. докстринг модуля): текстовый признак «слово мутаци* есть в
    тестовом файле», не повторный прогон мутации (снять фикс → красный →
    вернуть → зелёный) — тот прогон дороже, чем оправдано для этой узкой
    проверки, следующий шаг, если понадобится точнее, — отдельная задача."""
    guard_full = repo_root / guard_path
    if not guard_full.exists():
        return "fail", f"{guard_path} отсутствует на main"
    content = guard_full.read_text(encoding="utf-8")
    match = _PYTEST_INVOCATION_RE.search(content)
    if not match:
        return "pending", f"{guard_path} не называет тестовый файл через pytest — проверка вручную"
    test_rel = match.group(1)
    test_path = repo_root / test_rel
    if not test_path.exists():
        return "fail", f"{test_rel} (запускается {guard_path}) отсутствует на main"
    test_content = test_path.read_text(encoding="utf-8").lower()
    if "мутаци" not in test_content:
        return "pending", f"{test_rel} существует, но текстовый признак мутации не найден — проверить вручную"
    return "ok", f"{guard_path} → {test_rel}: файлы на месте, текстовый признак мутации найден"


def referenced_change_archived(text: str, repo_root: Path) -> bool | None:
    """True/False — если тело называет путь `openspec/changes/<slug>`,
    архивирован ли он (каталог `openspec/changes/archive/<slug>` существует,
    исходный `openspec/changes/<slug>` — нет). `None` — тело не называет
    такой путь, проверка неприменима (не гейтит закрытие)."""
    match = _CHANGE_PATH_RE.search(text or "")
    if not match:
        return None
    slug = match.group(1)
    if slug == "archive":
        return None
    active = repo_root / "openspec" / "changes" / slug
    archived = repo_root / "openspec" / "changes" / "archive" / slug
    if not active.exists() and not archived.exists():
        return None  # путь называет каталог, которого нет ни там ни там — не о change
    return (not active.exists()) and archived.exists()


def combine_evidence(base: tuple[str, str], extra: tuple[str, str]) -> tuple[str, str]:
    """Хуже из двух исходов (fail > pending > ok/docs) побеждает; детали
    складываются, не теряются (наблюдаемость — не молчаливый выбор одной из
    двух улик)."""
    base_state, base_detail = base
    extra_state, extra_detail = extra
    if _EVIDENCE_SEVERITY[extra_state] > _EVIDENCE_SEVERITY[base_state]:
        return extra_state, f"{extra_detail}; {base_detail}"
    return base_state, f"{base_detail}; {extra_detail}"


def decide_without_evidence(
    task_issue: dict,
    pull: dict,
    *,
    open_pulls: list[dict],
    already_marked: bool,
) -> dict | None:
    """Первая, ДЕШЁВАЯ фаза решения — не требует ни улики
    (`compute_evidence`), ни проверки предка main (`merge_commit_on_main`):
    обе стоят сетевых вызовов, а задача, отклонённая уже здесь (эпик, #120,
    без метки `task`, уже закрыта, маркер уже стоит, назначен исполнитель,
    конкурирующий PR), не должна их оплачивать. `None` — эта фаза не нашла
    причины пропустить, вызывающий обязан продолжить второй фазой
    (`decide_with_evidence`); `run()` использует это как короткое замыкание,
    `decide()` ниже — как первый из двух шагов единого чистого решения
    (нужен там, где вызывающему проще один вызов, например тестам)."""
    number = task_issue["number"]
    pr_number = pull["number"]
    if number == WATCHDOG_ISSUE:
        return _skip(number, pr_number, SKIP_WATCHDOG,
                     "issue #120 — постоянный канал эскалации, не задача пула")
    if task_issue.get("state") != "open":
        return _skip(number, pr_number, SKIP_NOT_OPEN, "задача уже не в состоянии open")
    if scheduler.is_epic_issue(task_issue):
        return _skip(number, pr_number, SKIP_EPIC,
                     "эпик — открытость означает незавершённость дочерних задач, не закрывается автоматом")
    if TASK_LABEL not in {label["name"] for label in task_issue.get("labels") or []}:
        return _skip(number, pr_number, SKIP_TASK_LABEL_MISSING, "issue без метки task — не из пула")
    if already_marked:
        return _skip(number, pr_number, SKIP_ALREADY,
                     f"маркер приёмки-по-ссылке для PR #{pr_number} уже стоит")
    if task_issue.get("assignees"):
        who = ", ".join(a["login"] for a in task_issue["assignees"])
        return _skip(number, pr_number, SKIP_ASSIGNEE, f"назначен исполнитель: {who}")
    competing = competing_open_pr(number, open_pulls)
    if competing is not None:
        return _skip(number, pr_number, SKIP_COMPETING_PR,
                     f"открыт PR #{competing}, чья ветка называет эту же задачу")
    return None


def decide_with_evidence(
    task_issue: dict,
    pull: dict,
    *,
    evidence_state: str,
    evidence_detail: str,
    merge_commit_on_main: bool,
) -> dict:
    """Вторая фаза — вызывается, только если первая (`decide_without_evidence`)
    вернула `None`. Требует уже посчитанной улики и проверки предка main."""
    number = task_issue["number"]
    pr_number = pull["number"]
    if not merge_commit_on_main:
        return _skip(number, pr_number, SKIP_NOT_ANCESTOR,
                     "коммит слияния PR не подтверждён предком main (класс #925) — не закрываю")
    if evidence_state == "pending":
        return _skip(number, pr_number, SKIP_EVIDENCE_PENDING, evidence_detail)
    if evidence_state == "fail":
        return _skip(number, pr_number, SKIP_EVIDENCE_FAIL, evidence_detail)
    return {"action": "close", "number": number, "pr": pr_number, "detail": evidence_detail}


def decide(
    task_issue: dict,
    pull: dict,
    *,
    open_pulls: list[dict],
    evidence_state: str,
    evidence_detail: str,
    already_marked: bool,
    merge_commit_on_main: bool,
) -> dict:
    """Единое чистое решение для ОДНОЙ пары (задача, PR) — обе фазы подряд,
    удобно там, где улика и так уже есть на входе (тесты, разовый разбор
    одного кандидата). `run()` вызывает фазы раздельно, чтобы не платить
    сетью за задачи, отклонённые первой, дешёвой фазой."""
    skip = decide_without_evidence(task_issue, pull, open_pulls=open_pulls, already_marked=already_marked)
    if skip is not None:
        return skip
    return decide_with_evidence(
        task_issue, pull,
        evidence_state=evidence_state, evidence_detail=evidence_detail,
        merge_commit_on_main=merge_commit_on_main,
    )


def _skip(number: int, pr_number: int, reason: str, detail: str) -> dict:
    return {"action": "skip", "number": number, "pr": pr_number, "reason": reason, "detail": detail}


def closure_comment(number: int, pr_number: int, detail: str) -> str:
    return (
        f"{REFERENCE_CLOSURE_MARKER} PR #{pr_number}\n\n"
        f"Слитый PR #{pr_number} прозой заявляет эту задачу (#{number}) закрытой "
        "(строгий маркер «закрывает/закрыл/закрыт #N», не имя ветки — второй "
        "источник связи PR→задача, #1042). Проверено машинно:\n\n"
        f"- {detail}\n"
        "- открытого PR/исполнителя на эту задачу нет.\n\n"
        "Закрываю."
    )


# ══════════════════════════════════════════════════════════════════════════
# Тонкая IO-обвязка
# ══════════════════════════════════════════════════════════════════════════


def merge_commit_on_main(repo: str, merge_commit_sha: str | None) -> bool:
    """True — merge_commit_sha реально предок (или равен) текущего main
    (#925: живая дыра, «зелёный прогон» цитировался для sha, не входящего в
    main). GitHub compare API: `status` "identical"/"behind" — sha уже в
    main; "ahead"/"diverged" — нет. RuntimeError (сеть/рейт-лимит — класс
    #454, а также неожиданный ответ API) НЕ гасится в False: «проверка
    сломана» и «предок не подтверждён» — разные состояния, лечатся по-разному
    (эскалация против отказа закрывать — AGENTS.md, «Алерт не гадает»);
    исключение поднимается вызывающему, тот уходит в эскалационную ветку
    REFERENCE_CLOSURE_ERROR_MARKER (#120), как уже делает для сбоя
    compute_evidence. False — только пустой merge_commit_sha и ответ
    "ahead"/"diverged"."""
    if not merge_commit_sha:
        return False
    payload = gh(f"repos/{repo}/compare/main...{merge_commit_sha}")
    status = (payload or {}).get("status")
    if status in ("identical", "behind"):
        return True
    if status in ("ahead", "diverged"):
        return False
    raise RuntimeError(
        f"compare main...{merge_commit_sha}: неожиданный ответ API (status={status!r})")


def compute_evidence(repo: str, repo_root: Path, pull: dict) -> tuple[str, str]:
    """('ok'|'docs'|'fail'|'pending', детали) — те же категории и функции,
    что штатная приёмка (`scheduler.classify_acceptance`/`deploy_evidence`/
    `script_evidence`/`docs_missing`), плюс гвардия-каталога (см. докстринг
    модуля). RuntimeError (инфраструктурный сбой, не «улика красная») не
    гасится — поднимается вызывающему, тот эскалирует, а не тихо считает
    провалом (тот же приём, что и accept_merged_tasks)."""
    files_payload = review_labels.list_pr_files(repo, pull["number"], gh)
    filenames = [entry["filename"] for entry in files_payload]
    category = scheduler.classify_acceptance(filenames)
    if category == scheduler.ACCEPT_DOCS:
        missing = scheduler.docs_missing(repo, files_payload)
        if missing:
            base = ("fail", f"файлы отсутствуют в main: {', '.join(missing)}")
        else:
            checked = [f["filename"] for f in files_payload if f.get("status") != "removed"]
            base = (("docs", f"файлы на месте в main: {', '.join(checked)}") if checked
                     else ("docs", "правка — только удаления (архивация), физической проверки нет"))
    elif category == scheduler.ACCEPT_DEPLOY:
        merged_at = parse_time(pull["merged_at"])
        base = scheduler.deploy_evidence(repo, merged_at, pull.get("merge_commit_sha"))
    else:
        base = scheduler.script_evidence(repo, pull["head"]["sha"])

    for guard_path in guard_paths(filenames):
        base = combine_evidence(base, guard_test_evidence(repo_root, guard_path))

    change_archived = referenced_change_archived(pull.get("body") or "", repo_root)
    if change_archived is False:
        base = combine_evidence(base, ("fail", "тело называет openspec/changes/<id>, ещё не заархивирован"))
    elif change_archived is True:
        base = combine_evidence(base, ("ok", "названный openspec/changes/<id> заархивирован"))

    return base


def already_reference_marked(repo: str, task_number: int, pr_number: int) -> bool:
    marker = f"{REFERENCE_CLOSURE_MARKER} PR #{pr_number}"
    return bool(issue_marker_times(repo, task_number, marker))


def run(repo: str) -> list[str]:
    """Один живой прогон. Возвращает строки отчёта — КАЖДЫЙ кандидат печатан
    ровно одной строкой, закрыт он или пропущен и почему (наблюдаемость —
    прямое требование задачи #1042, молчаливый пропуск запрещён)."""
    lines: list[str] = []
    merged_pulls = scheduler.all_merged_pulls(repo)
    candidates = find_candidates(merged_pulls)
    if not candidates:
        return ["💗 приёмка-по-ссылке: кандидатов (задач, заявленных чужим слитым PR) не найдено"]

    open_pulls = scheduler.open_pulls(repo)
    closed_count = 0
    for number, pull in sorted(candidates.items()):
        pr_number = pull["number"]
        try:
            task_issue = gh(f"repos/{repo}/issues/{number}")
        except RuntimeError as error:
            lines.append(f"⚠️ #{number}: не удалось прочитать issue — {error}")
            continue
        if task_issue is None or "pull_request" in task_issue:
            lines.append(f"⚠️ #{number}: не issue задачи (PR или отсутствует) — пропуск")
            continue

        if closed_count >= REFERENCE_CLOSURE_RUN_CAP:
            lines.append(
                f"🛑 #{number}: потолок {REFERENCE_CLOSURE_RUN_CAP} закрытий за прогон исчерпан "
                f"(PR #{pr_number}) — рассмотрю на следующем прогоне ({SKIP_CAP})")
            continue

        try:
            already_marked = already_reference_marked(repo, number, pr_number)
        except RuntimeError as error:
            lines.append(f"⚠️ #{number}: комментарии не прочитаны, приёмка-по-ссылке отложена — {error}")
            continue

        # Дешёвая фаза первой (см. decide_without_evidence) — эпик/#120/
        # без метки task/уже закрыта/маркер уже стоит/назначен исполнитель/
        # конкурирующий PR не должны оплачивать сеть за улику и compare API.
        decision = decide_without_evidence(
            task_issue, pull, open_pulls=open_pulls, already_marked=already_marked)
        if decision is None:
            try:
                on_main = merge_commit_on_main(repo, pull.get("merge_commit_sha"))
                evidence_state, evidence_detail = compute_evidence(repo, REPO_ROOT, pull)
            except RuntimeError as error:
                error_marker = f"{REFERENCE_CLOSURE_ERROR_MARKER} #{number} PR #{pr_number}"
                text = f"🚨 #{number}: приёмка-по-ссылке PR #{pr_number} не смогла проверить улику — {error}"
                try:
                    already_escalated = issue_marker_times(repo, WATCHDOG_ISSUE, error_marker)
                except RuntimeError:
                    already_escalated = []
                if already_escalated:
                    lines.append(f"{text} (уже эскалировано, повторно не шлём)")
                else:
                    delivered = escalate(repo, WATCHDOG_ISSUE, f"{error_marker} {text}")
                    lines.append(f"{text} ({delivered})")
                continue
            decision = decide_with_evidence(
                task_issue, pull,
                evidence_state=evidence_state, evidence_detail=evidence_detail,
                merge_commit_on_main=on_main,
            )
        if decision["action"] == "skip":
            lines.append(f"⏭️ #{number}: не закрываю (PR #{pr_number}, {decision['reason']}) — {decision['detail']}")
            continue

        comment = closure_comment(number, pr_number, decision["detail"])
        # Закрытие ПЕРВЫМ, маркер-комментарий вторым (блокирующая находка
        # ревью PR #1046). Обратный порядок замораживал кандидата навсегда:
        # комментарий ушёл, PATCH упал (транзиент/рейт-лимит — класс #454),
        # следующий прогон видел маркер и отвечал SKIP_ALREADY — задача
        # оставалась открыта с комментарием «Закрываю.», ни перезапуска
        # закрытия, ни эскалации (silent-wrong). В прямом порядке сбой
        # PATCH оставляет задачу открытой и БЕЗ маркера — следующий прогон
        # повторяет попытку целиком; сбой комментария при уже закрытой
        # задаче — громкая ⚠️-строка, а не отложенный кандидат: закрытую
        # задачу следующий прогон отсекает по state != open (SKIP_NOT_OPEN),
        # перезакрытия и дублей комментария нет.
        try:
            gh("-X", "PATCH", f"repos/{repo}/issues/{number}", "-f", "state=closed")
        except RuntimeError as error:
            lines.append(
                f"⚠️ #{number}: закрытие не выполнено, маркер-комментарий не ставился — "
                f"повтор на следующем прогоне — {error}")
            continue
        try:
            post_issue_comment(repo, number, comment)
        except RuntimeError as error:
            lines.append(
                f"⚠️ #{number}: задача ЗАКРЫТА, но комментарий-улика с маркером "
                f"не доставлен — доказательство остаётся только в отчёте этого "
                f"прогона — {error}")
        try:
            lines.append(f"🔓 {claim_task.release(repo, number)}")
        except RuntimeError as error:
            lines.append(f"⚠️ замок task-{number} не снят: {error}")
        closed_count += 1
        lines.append(f"✅ #{number}: закрыта по ссылке из PR #{pr_number} — {decision['detail']}")

    return lines


def exit_code(lines: list[str]) -> int:
    """Код выхода прогона (см. раздел докстринга «Носитель вызова»):
    1 — незакрытая громкая строка, ⚠️ (операция не удалась) или НОВЫЙ
    🚨-эпизод (проверка сломана, эскалация #120 только что отправлена);
    0 — тихие исходы (💗/⏭️/✅/🛑) и ПОВТОРНЫЙ 🚨 («уже эскалировано»,
    дедуп на эпизод — газ: доставленный алерт не краснит каждый следующий
    прогон). Строки — единственный носитель исхода между run() и main(),
    поэтому распознавание повторного эпизода идёт по тексту самой строки,
    который run() формирует ровно в двух местах ветки эскалации."""
    for line in lines:
        if line.startswith("⚠️"):
            return 1
        if line.startswith("🚨") and "уже эскалировано" not in line:
            return 1
    return 0


def main() -> int:
    repo = os.environ.get("GITHUB_REPOSITORY", "mytab0r/edge-harness")
    lines = run(repo)
    for line in lines:
        print(line)
    return exit_code(lines)


if __name__ == "__main__":
    sys.exit(main())
