#!/usr/bin/env python3
"""Инварианты состояния репозитория (#244): «такого состояния быть не должно».

Мета-течь, которую закрывает этот файл: за 2026-09-02/03 в конвейере нашли
~15 дефектов, и НИ ОДИН не нашла система — все нашли люди/агенты, читая логи.
Два уже существующих механизма ловят СВОЙ узкий класс отлично (белый спот без
task — repo-ci.yml, #179; реестр меток — scripts/lib/test_label_registry.py,
#207) и ничего больше — потому что каждый инвариант писался отдельно, без
общего дома. Этот файл — общий дом для проверок вида «такого состояния
репозитория быть не должно», по образцу scripts/orchestra/pulse_guard.py
(чистые decide_* функции + тонкая IO-обвязка на gh()), а не третий
параллельный механизм.

Каждая check_* функция принимает уже загруженные данные (без сети — тестируется
на прод-форме фикстур, доказывается мутацией) и возвращает список нарушений
(list[dict]), пустой список — здоровое состояние. IO ниже собирает данные через
gh() (общий с pulse_guard/scheduler, тот же субпроцесс-контракт) и печатает
отчёт; мутирующие вызовы (escalate) — только когда violations непусты.

Пять инвариантов первой волны:
  1. check_reopened_after_merge — открытая задача task без исполнителя,
     чей PR уже слит: воркер/scheduler.dispatch_worker выберут её снова
     (класс #18/#21/#78 при слитых PR #138/#177/#163).
  2. check_free_task_count_mismatch — «истинное» число свободных задач
     (без assignee) расходится с тем, что реально выберет
     scripts/worker/task.sh::free_task() (баг класса «substring-scan по
     всему телу открытых PR»). Файл не трогаем — паттерн scan("...")
     вытаскивается из него же в рантайме.
  3. check_stuck_review_gate — review:ok стоит дольше порога без НИКАКОГО
     ai:*-вердикта (класс #147, сутки простоя). Порог — существующее место
     правды pulse_guard.UNHEALTHY_PR_AFTER_MINUTES, своего числа не заводим.
  4. check_unarchived_complete_changes — openspec/changes/<id>/tasks.md
     полностью отмечен, а каталог не в openspec/changes/archive/.
  5. check_duplicate_evidence — два открытых task-issue ссылаются в теле на
     один и тот же file:line (класс #202/#213/#212). Честный потолок ниже.

Расписание: главный канал — периодический шаг orchestra.yml (cron */15 мин),
он же вызывает escalate() для инвариантов 1 и 3 (см. docstring escalate_*).
Дополнительно repo-ci.yml печатает тот же отчёт на каждый push/PR (видимость
раньше следующего пульса), но НЕ проваливает обязательную проверку `test`:
пять инвариантов проверяют СОСТОЯНИЕ РЕПОЗИТОРИЯ (issues/PR/openspec), а не
дифф текущего PR — обвал состояния, накопленный за месяцы, не вина автора
этого конкретного пуша, и превращать его в требование «почини чужой бэклог,
чтобы слить свой PR» было бы третьим по счёту тормозом без объявленного газа
(AGENTS.md, правило «Тормоз без газа не принимается»). Замер на живом
репозитории 2026-09-03 (см. README PR): инварианты 1/2/5 уже находят реальный
накопленный долг (17/16/7 нарушений) — сделать их required-гейтом немедленно
означало бы покрасить main для всех агентов из-за чужого долга. Решение —
у владельца: включить гейт можно, изменив CI_GATING ниже, после разбора
бэклога.

Запуск:
  python scripts/orchestra/repo_invariants.py             # печать отчёта (repo-ci.yml)
  python scripts/orchestra/repo_invariants.py --orchestra  # печать + escalate (orchestra.yml)
"""

import argparse
import importlib.util
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# gh()/parse_time()/minutes_between()/escalate()/WATCHDOG_ISSUE — одно место
# правды в pulse_guard (тот же субпроцесс-контракт gh api, тот же канал
# эскалации #120 + Telegram, третий канал не заводим).
_PG_SPEC = importlib.util.spec_from_file_location(
    "pulse_guard", Path(__file__).resolve().parent / "pulse_guard.py")
pulse_guard = importlib.util.module_from_spec(_PG_SPEC)
_PG_SPEC.loader.exec_module(pulse_guard)  # type: ignore[union-attr]

gh = pulse_guard.gh
parse_time = pulse_guard.parse_time
minutes_between = pulse_guard.minutes_between
escalate = pulse_guard.escalate
issue_marker_times = pulse_guard.issue_marker_times
WATCHDOG_ISSUE = pulse_guard.WATCHDOG_ISSUE
# «PR нездоров дольше этого — действуй» — уже объявленный порог для «состояние
# держится слишком долго» (scheduler.py, #196). Инварианты 1 и 3 переиспользуют
# его как порог эскалации, а не заводят своё число.
UNHEALTHY_PR_AFTER_MINUTES = pulse_guard.UNHEALTHY_PR_AFTER_MINUTES

_RL_SPEC = importlib.util.spec_from_file_location(
    "review_labels", REPO_ROOT / "scripts" / "lib" / "review_labels.py")
review_labels = importlib.util.module_from_spec(_RL_SPEC)
_RL_SPEC.loader.exec_module(review_labels)  # type: ignore[union-attr]

TASK_LABEL = "task"
TASK_SH = REPO_ROOT / "scripts" / "worker" / "task.sh"
OPENSPEC_CHANGES = REPO_ROOT / "openspec" / "changes"

# Инварианты, чьё нарушение сейчас (2026-09-03) уже есть на живом main —
# см. docstring выше. Требуется явная правка этой константы владельцем, чтобы
# инвариант начал ронять обязательную проверку repo-ci.yml `test`.
CI_GATING: frozenset[int] = frozenset()


# ══════════════════════════════════════════════════════════════════════════
# Инвариант 1: открытая задача без исполнителя, чей PR уже слит
# ══════════════════════════════════════════════════════════════════════════

# Первая НЕпустая строка тела PR, если это РОВНО `#N` и больше ничего.
# Строже task_ref.declared_tasks (который матчит ЛЮБУЮ строку, начинающуюся
# с `#`, — заголовки `## Что сделано` не страдают, там нет цифр сразу после
# `#`, но живая находка на PR #137 показала другой случай: перенос строки
# внутри абзаца прозы уронил ` #119 из пула...` на новую строку, и
# declared_tasks() принял её за декларацию — задача #119 никогда не
# объявлялась PR #137 первой строкой, реальная первая строка — `#18`. Здесь
# те же слова, что и во всех живых декларациях (#18, #205, #207, #80…) —
# ровно `#N`, ничего больше на строке.
_BARE_TASK_REF_RE = re.compile(r"#(\d+)")


def primary_declared_task(body: str) -> int | None:
    if not body:
        return None
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = re.fullmatch(_BARE_TASK_REF_RE, stripped)
        return int(match.group(1)) if match else None
    return None


def check_reopened_after_merge(open_tasks: list[dict], merged_pulls: list[dict]) -> list[dict]:
    """Открытая задача task без assignee, для которой уже есть слитый PR,
    декларировавший её первой строкой. Механизм инцидента: reap_stale
    (scheduler.py) смотрит только ОТКРЫТЫЕ PR — если PR уже слит, «нет
    открытого PR, ссылающегося на задачу» читается как «работа брошена», и
    assignee снимается ДАЖЕ когда работа честно завершена, просто исполнитель
    ещё не сделал пост-мерж проверку и не закрыл issue. Свободная задача с
    уже слитым PR — именно то состояние, в котором free_task()/
    dispatch_worker выберут её заново (класс #18/#21/#78)."""
    unassigned = {t["number"]: t for t in open_tasks if not t["assignees"]}
    by_task: dict[int, list[dict]] = {}
    for pull in merged_pulls:
        declared = primary_declared_task(pull.get("body") or "")
        if declared is not None:
            by_task.setdefault(declared, []).append(pull)

    violations = []
    for number, pulls in sorted(by_task.items()):
        if number not in unassigned:
            continue
        newest = max(pulls, key=lambda p: p["merged_at"])
        violations.append({
            "issue": number,
            "title": unassigned[number]["title"],
            "prs": sorted(p["number"] for p in pulls),
            "merged_at": newest["merged_at"],
        })
    return violations


# ══════════════════════════════════════════════════════════════════════════
# Инвариант 2: число свободных задач — два независимых метода расходятся
# ══════════════════════════════════════════════════════════════════════════


def extract_task_sh_scan_pattern(text: str) -> str:
    """Паттерн, которым task.sh::free_task() решает «эта задача занята
    открытым PR» — вытащен из ФАЙЛА в рантайме, не хардкожен: если task.sh
    когда-нибудь поправят (баг класса «substring-scan», сейчас в отдельном
    PR — этот файл мы не трогаем), инвариант сам увидит новый паттерн и
    сам замолчит, без правки здесь."""
    match = re.search(r'scan\("([^"]+)"\)', text)
    if not match:
        raise RuntimeError(
            "scripts/worker/task.sh: не нашёл scan(\"...\") в free_task() — "
            "скрипт переписан, обнови extract_task_sh_scan_pattern"
        )
    return match.group(1)


def check_free_task_count_mismatch(
    open_tasks: list[dict], open_pulls: list[dict], task_sh_text: str,
) -> dict | None:
    """Метод A (истина пула — то же, чем пользуется scheduler.dispatch_worker):
    открытые task-issue без assignee. Метод B — то же множество, из которого
    вычтены номера, которые task.sh::free_task() посчитает «занятыми»: паттерн
    scan(...) применяется к телам ВСЕХ открытых PR буквально так же, как это
    делает сам скрипт (subprocess его не трогаем, регэксп читаем из файла).
    Расхождение A и B — ложно заблокированные номера: воркер не возьмёт
    задачу, которая на самом деле свободна, потому что её номер просто
    УПОМЯНУТ в чужом PR (баг класса substring-scan, а не декларация)."""
    ground_truth = sorted(t["number"] for t in open_tasks if not t["assignees"])
    pattern = extract_task_sh_scan_pattern(task_sh_text)
    taken: set[int] = set()
    for pull in open_pulls:
        for match in re.finditer(pattern, pull.get("body") or ""):
            digits = match.group(0).lstrip("#")
            if digits.isdigit():
                taken.add(int(digits))
    worker_would_pick = sorted(n for n in ground_truth if n not in taken)
    falsely_blocked = sorted(set(ground_truth) - set(worker_would_pick))
    if not falsely_blocked:
        return None
    return {
        "ground_truth_count": len(ground_truth),
        "worker_count": len(worker_would_pick),
        "falsely_blocked": falsely_blocked,
    }


# ══════════════════════════════════════════════════════════════════════════
# Инвариант 3: review:ok без вердикта ai:* дольше порога
# ══════════════════════════════════════════════════════════════════════════


def last_review_ok_labeled_at(repo: str, pr_number: int) -> datetime | None:
    """Тот же приём, что scheduler.last_review_ok_labeled_at (#196) — не
    импортируем scheduler.py (по нему открытый PR другого агента, не трогаем
    файл), логика достаточно мала (скан таймлайна), чтобы держать копию
    локально было безопаснее, чем зависеть от чужого модуля в разработке."""
    timeline = gh(f"repos/{repo}/issues/{pr_number}/timeline?per_page=100")
    labeled_at = [
        event["created_at"] for event in timeline
        if event.get("event") == "labeled"
        and (event.get("label") or {}).get("name") == review_labels.REVIEW_OK
    ]
    return parse_time(max(labeled_at)) if labeled_at else None


def check_stuck_review_gate(repo: str, now: datetime, open_pulls: list[dict]) -> list[dict]:
    """review:ok стоит дольше UNHEALTHY_PR_AFTER_MINUTES, и НИ ОДНОЙ ai:*
    метки ещё нет. scheduler.trigger_ai_review (#196) уже пытается сам
    перезапустить ai-review.yml на меньшем пороге
    (AI_REVIEW_RETRY_AFTER_MINUTES) с ограничением попыток
    (AI_REVIEW_MAX_ATTEMPTS) — этот инвариант ловит случай, когда газ #196
    исчерпал попытки, не сработал вовсе (оркестратор не бежал) или ещё не
    было задеплоено — застрявший гейт, класс #147 (сутки простоя)."""
    ai_labels = set(review_labels.AI_VERDICTS)
    violations = []
    for pull in open_pulls:
        labels = {label["name"] for label in pull["labels"]}
        if review_labels.REVIEW_OK not in labels or labels & ai_labels:
            continue
        labeled_at = last_review_ok_labeled_at(repo, pull["number"])
        if labeled_at is None:
            continue
        age = minutes_between(labeled_at, now)
        if age <= UNHEALTHY_PR_AFTER_MINUTES:
            continue
        violations.append({
            "pr": pull["number"],
            "age_minutes": round(age, 1),
            "labeled_at": labeled_at.isoformat(),
        })
    return violations


# ══════════════════════════════════════════════════════════════════════════
# Инвариант 4: openspec/changes/<id> полностью отмечен и не заархивирован
# ══════════════════════════════════════════════════════════════════════════

_CHECKBOX_RE = re.compile(r"^\s*-\s*\[([ xX])\]", re.MULTILINE)


def check_unarchived_complete_changes(changes_dir: Path) -> list[dict]:
    """Каталог openspec/changes/<id>/tasks.md, где ВСЕ чекбоксы отмечены
    (и их хотя бы один), а сам каталог лежит не под openspec/changes/archive/
    (её пока не существует вовсе — задокументированный факт задачи #244)."""
    if not changes_dir.is_dir():
        return []
    violations = []
    for entry in sorted(changes_dir.iterdir()):
        if not entry.is_dir() or entry.name == "archive":
            continue
        tasks_md = entry / "tasks.md"
        if not tasks_md.exists():
            continue
        boxes = _CHECKBOX_RE.findall(tasks_md.read_text(encoding="utf-8"))
        if boxes and all(box.lower() == "x" for box in boxes):
            violations.append({"change": entry.name, "checked": len(boxes)})
    return violations


# ══════════════════════════════════════════════════════════════════════════
# Инвариант 5: два открытых task-issue ссылаются на один и тот же file:line
# ══════════════════════════════════════════════════════════════════════════

# Честный потолок (в стиле docs/agents/LABELS.md): сигнал берётся по ФОРМЕ
# ссылки в обратных кавычках — `path/to/file.ext:NNN` или `...:NNN-MMM`, а не
# по тексту заголовка раздела. Живые данные используют вперемешку «Факты»,
# «Факт», «Где», «Где именно (file:line)» или вовсе без заголовка (#222) —
# заголовок не единое место, поэтому матчинг по нему был бы хрупким по
# конструкции. Расширения ограничены исходным кодом (py/sh/ts/tsx/js/mjs/
# yml/yaml) — документация (.md) сознательно исключена: замер на живом
# репозитории показал ложную склейку #244/#201 и #219/#215 через `AGENTS.md`
# и через символ `escalate` — оба цитируют общее место (канонический раздел
# правил, инфраструктурную функцию) как ПОДДЕРЖИВАЮЩИЙ контекст, а не как
# адрес бага. Символьные ссылки (`file.py::func`, `` `file.py` — `func()` ``)
# рассматривались и отклонены по той же причине: `pulse_guard.py::escalate`
# оказался ложным совпадением между #244 и #201 при том же замере — точное
# совпадение file:line в КОДЕ достаточно редкое совпадение, чтобы быть
# сигналом; совпадение имени часто используемой функции — нет. Следствие:
# пара #222/#149 (символьные, не числовые ссылки) этим инвариантом не
# ловится — дисциплина автора/ревьюера, не механическая проверка.
_CODE_EXTENSIONS = r"(?:py|sh|ts|tsx|js|mjs|yml|yaml)"
_LOCATOR_RE = re.compile(rf"`([\w./-]+\.{_CODE_EXTENSIONS}):(\d+)(?:-(\d+))?`")


def extract_locators(body: str) -> set[tuple[str, int, int]]:
    if not body:
        return set()
    locators = set()
    for line in body.splitlines():
        for match in _LOCATOR_RE.finditer(line):
            file_, start, end = match.group(1), int(match.group(2)), match.group(3)
            locators.add((file_, start, int(end) if end else start))
    return locators


def _locators_overlap(a: tuple[str, int, int], b: tuple[str, int, int]) -> bool:
    file_a, start_a, end_a = a
    file_b, start_b, end_b = b
    return file_a == file_b and start_a <= end_b and start_b <= end_a


def check_duplicate_evidence(open_tasks: list[dict]) -> list[dict]:
    """Два открытых task-issue, чьи тела ссылаются на пересекающийся диапазон
    строк одного файла (класс #202/#213/#212: contract_check.py разбирался
    трижды под разными номерами, каждый раз заново)."""
    per_issue = [
        (t["number"], t["title"], extract_locators(t.get("body") or ""))
        for t in open_tasks
    ]
    violations = []
    for i in range(len(per_issue)):
        num_a, title_a, locs_a = per_issue[i]
        if not locs_a:
            continue
        for j in range(i + 1, len(per_issue)):
            num_b, title_b, locs_b = per_issue[j]
            if not locs_b:
                continue
            shared = next(
                (
                    (a, b) for a in locs_a for b in locs_b
                    if _locators_overlap(a, b)
                ),
                None,
            )
            if shared is None:
                continue
            violations.append({
                "issues": [num_a, num_b],
                "titles": [title_a, title_b],
                "shared_location": f"{shared[0][0]}:{shared[0][1]}-{shared[0][2]}",
            })
    return violations


# ══════════════════════════════════════════════════════════════════════════
# IO: сбор данных, отчёт, эскалация
# ══════════════════════════════════════════════════════════════════════════


def fetch_open_task_issues(repo: str) -> list[dict]:
    payload = gh(f"repos/{repo}/issues?state=open&labels={TASK_LABEL}&per_page=100")
    return [issue for issue in payload if "pull_request" not in issue]


def fetch_merged_pulls(repo: str, max_pages: int = 5) -> list[dict]:
    """Слитые PR — REST отдаёт state=closed вперемешку с незлитыми, отбираем
    по merged_at. max_pages=5 (500 PR) — с запасом покрывает весь репозиторий
    на момент задачи #244 (98 слитых всего); если PR станет больше, инвариант
    честно недосмотрит самые старые, а не упадёт — тот же компромисс, что уже
    принят в scheduler.py (per_page=100 без пагинации совсем)."""
    results = []
    for page in range(1, max_pages + 1):
        batch = gh(
            f"repos/{repo}/pulls?state=closed&per_page=100&page={page}"
            "&sort=updated&direction=desc"
        )
        if not batch:
            break
        results.extend(pull for pull in batch if pull.get("merged_at"))
        if len(batch) < 100:
            break
    return results


def fetch_open_pulls(repo: str) -> list[dict]:
    return gh(f"repos/{repo}/pulls?state=open&per_page=100")


def build_report(repo: str, now: datetime) -> tuple[list[str], dict[int, list]]:
    """Возвращает (строки отчёта, {номер_инварианта: violations}). Чистых
    мутирующих вызовов здесь нет — только GET (см. gh()); гвардия холостого
    хода проверяет именно это."""
    open_tasks = fetch_open_task_issues(repo)
    merged_pulls = fetch_merged_pulls(repo)
    open_pulls = fetch_open_pulls(repo)

    findings: dict[int, list] = {}
    lines = ["## Инварианты состояния репозитория (#244)"]

    v1 = check_reopened_after_merge(open_tasks, merged_pulls)
    findings[1] = v1
    if v1:
        lines.append(f"🚨 [1] {len(v1)} открытых задач без исполнителя с уже слитым PR:")
        for item in v1:
            lines.append(
                f"   — #{item['issue']} «{item['title']}» — слит PR"
                f" {', '.join('#' + str(n) for n in item['prs'])} ({item['merged_at']})"
            )
    else:
        lines.append("💚 [1] нет открытых задач с уже слитым PR")

    v2_error = None
    try:
        task_sh_text = TASK_SH.read_text(encoding="utf-8")
        v2 = check_free_task_count_mismatch(open_tasks, open_pulls, task_sh_text)
    except (OSError, RuntimeError) as error:
        v2 = None
        v2_error = error
    findings[2] = [v2] if v2 else []
    if v2_error is not None:
        lines.append(f"⚠️ [2] не удалось проверить (fail loud, не молчим): {v2_error}")
    elif v2:
        lines.append(
            f"🚨 [2] свободных задач по факту {v2['ground_truth_count']}, "
            f"воркер выберет из {v2['worker_count']} — ложно заблокированы: "
            + ", ".join(f"#{n}" for n in v2['falsely_blocked'])
        )
    else:
        lines.append("💚 [2] число свободных задач совпадает по обоим методам")

    v3 = check_stuck_review_gate(repo, now, open_pulls)
    findings[3] = v3
    if v3:
        lines.append(f"🚨 [3] {len(v3)} открытых PR с review:ok без вердикта ai:* дольше {UNHEALTHY_PR_AFTER_MINUTES} мин:")
        for item in v3:
            lines.append(f"   — PR #{item['pr']} — {int(item['age_minutes'])} мин без ai:*")
    else:
        lines.append("💚 [3] нет застрявших review:ok без ai:*")

    v4 = check_unarchived_complete_changes(OPENSPEC_CHANGES)
    findings[4] = v4
    if v4:
        lines.append(f"🚨 [4] {len(v4)} openspec/changes полностью отмечены и не заархивированы:")
        for item in v4:
            lines.append(f"   — openspec/changes/{item['change']} ({item['checked']} чекбоксов)")
    else:
        lines.append("💚 [4] нет полностью отмеченных незаархивированных change")

    v5 = check_duplicate_evidence(open_tasks)
    findings[5] = v5
    if v5:
        lines.append(f"🚨 [5] {len(v5)} пар открытых задач с пересекающейся уликой file:line:")
        for item in v5:
            a, b = item["issues"]
            lines.append(f"   — #{a} и #{b} — {item['shared_location']}")
    else:
        lines.append("💚 [5] нет открытых задач с пересекающейся уликой file:line")

    return lines, findings


def summary(lines: list[str]) -> None:
    text = "\n".join(lines) + "\n"
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as file:
            file.write(text)


ESCALATING_INVARIANTS = (1, 3)


def escalate_if_new(repo: str, invariant_id: int, marker_key: str, text: str) -> str | None:
    """Эскалация «один раз на состояние»: маркер кодирует конкретный набор
    нарушителей (не просто факт «инвариант N нарушен») — тот же приём, что
    pulse_guard.PAUSE_MARKER/HEARTBEAT_MARKER (issue_marker_times ищет
    подстроку). Набор изменился (новый нарушитель, старый пропал) — новый
    маркер, новая эскалация; тот же набор — тишина, конвейер не спамит
    Telegram каждые 15 минут одним и тем же списком."""
    marker = f"[инвариант {invariant_id}: {marker_key}]"
    try:
        if issue_marker_times(repo, WATCHDOG_ISSUE, marker):
            return None  # уже эскалировано именно это состояние
    except RuntimeError as error:
        print(f"::warning::не удалось прочитать маркеры #{WATCHDOG_ISSUE}: {error}", file=sys.stderr)
        return None
    return escalate(repo, WATCHDOG_ISSUE, f"{marker}\n{text}")


def run_escalations(repo: str, findings: dict[int, list]) -> list[str]:
    lines = []
    if findings.get(1):
        v1 = findings[1]
        key = ",".join(f"#{i['issue']}" for i in v1)
        text = (
            "🚨 edge-harness: инвариант 1 (задача открыта, PR уже слит) — "
            f"{len(v1)} задач без исполнителя с уже слитым PR: " + key + ". "
            "Воркер/диспетч может выбрать их снова (класс #18/#21/#78) — "
            "закрой после пост-мерж проверки или переназначь."
        )
        result = escalate_if_new(repo, 1, key, text)
        if result:
            lines.append(f"📣 инвариант 1 эскалирован: {result}")
    if findings.get(3):
        v3 = findings[3]
        key = ",".join(f"#{i['pr']}" for i in v3)
        text = (
            "🚨 edge-harness: инвариант 3 (застрявший гейт) — "
            f"{len(v3)} PR с review:ok без вердикта ai:* дольше "
            f"{UNHEALTHY_PR_AFTER_MINUTES} мин: " + key + ". "
            "Авто-повтор #196 либо исчерпал попытки, либо не сработал — нужен человек."
        )
        result = escalate_if_new(repo, 3, key, text)
        if result:
            lines.append(f"📣 инвариант 3 эскалирован: {result}")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--orchestra", action="store_true",
                         help="периодический режим: report + escalate (инварианты 1 и 3)")
    args = parser.parse_args()

    repo = os.environ["GITHUB_REPOSITORY"]
    now = datetime.now(timezone.utc)
    lines, findings = build_report(repo, now)

    if args.orchestra:
        lines += run_escalations(repo, findings)

    summary(lines)

    gating_violations = [n for n in CI_GATING if findings.get(n)]
    if gating_violations and not args.orchestra:
        print(f"::error::инварианты {gating_violations} нарушены и включены в CI_GATING")
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as error:
        print(f"::error::repo_invariants: {error}")
        sys.exit(1)
