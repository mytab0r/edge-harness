#!/usr/bin/env python3
"""Гвардия класса «страница GitHub API без обхода молча теряет хвост» (#308).

Класс: сырой вызов `gh(f"...per_page=100...")` (или `gh_func(...)`) БЕЗ параметра
`page=` читает ровно ОДНУ страницу списочного ответа GitHub API и рушится
именно там, где список самый длинный — то есть там, где это заметнее всего
и дороже всего чинится постфактум. За сутки этот класс чинили точечно семь
раз (`check_pr.py`, `ai_review.py::cmd_gather`/`cmd_verdict`,
`scheduler.py::after_merge`, `review_labels::latest_ai_comment`, таймлайн
`last_ready_labeled_at`/`last_review_ok_labeled_at`) — и каждый раз находилось
следующее место (последним ревью #253 названы `all_merged_pulls`/
`classify_acceptance`/`docs_missing`, ещё не закрытые). Эта гвардия делает
повтор невозможным механически: любой НОВЫЙ сырой одностраничный вызов вне
явно перечисленного и обоснованного ALLOWED_SINGLE_PAGE_CALLS — красный тест.

Признак: `per_page=` в f-строке, переданной напрямую в `gh(...)`/`gh_func(...)`,
БЕЗ отдельного параметра `page=` в той же строке (в `per_page=100` тоже есть
подстрока `page=`, поэтому проверка исключает вариант, где `page=` идёт сразу
после `per_` — см. PAGE_PARAM_RE). Вызов, который сам листает страницы (как
`review_labels.list_pr_files`/`list_timeline`/`latest_ai_comment` — см. их
`&page={page}` в URL), автоматически проходит: обход страниц уже встроен в
саму сигнатуру запроса, отдельно перечислять такие функции не нужно.

Разрешённый список ALLOWED_SINGLE_PAGE_CALLS — ОДНО место, где одностраничный
вызов признан безопасным осознанно (см. комментарий у каждой записи), а не
забытым обходом. Новое место, которому одностраничный вызов действительно
безопасен (список по природе ограничен) — назови причину и добавь запись
сюда; если список НЕ ограничен по природе — почини обходом (по образцу
review_labels.list_pr_files) или громким сбоем на полной странице (по образцу
upstream_drift.upstream_drift_check).

Запуск: python -m pytest scripts/lib/test_pagination_guard.py -q
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Сырой вызов гвардии (gh(f"...")/gh_func(f"...")) с per_page= в URL. \s* между
# именем функции и открывающей f-строкой намеренно перекрывает перенос строки
# (`\s` матчит `\n` без re.MULTILINE) — часть вызовов в репозитории оборачивает
# f-строку на следующую строку (см. pulse_guard.recent_runs).
CALL_RE = re.compile(r'gh(?:_func)?\(\s*f"([^"\n]*per_page=[^"\n]*)"')

# `per_page=100` сам содержит подстроку `page=100` — не признак пагинации.
# Настоящий признак обхода страниц — отдельный параметр `page=`, не идущий
# сразу после `per_`.
PAGE_PARAM_RE = re.compile(r"(?<!per_)page=")

DEF_RE = re.compile(r"^\s*def\s+(\w+)\s*\(")

# Одностраничные вызовы, признанные безопасными ОСОЗНАННО — список ограничен
# по природе или уже кричит громко на упоре в полную страницу, а не листает.
# Ключ — (относительный путь от корня репозитория, имя функции).
ALLOWED_SINGLE_PAGE_CALLS = {
    ("scripts/orchestra/pulse_guard.py", "recent_runs"):
        "per_page — параметр самой функции (вызывающие передают 5/10): это "
        "запрос «дай N последних прогонов», а не полный список — усечение "
        "здесь и есть контракт функции, не риск потери хвоста.",
    ("scripts/orchestra/pulse_guard.py", "last_failure_error"):
        "job'ы ОДНОГО прогона workflow — фиксированное малое число шагов "
        "конвейера (repo-ci.yml — не растущий пользователем список).",
    ("scripts/orchestra/upstream_drift.py", "upstream_drift_check"):
        "гвардия усечения уже в теле функции (#134): `if len(tags) >= 100: "
        "raise RuntimeError(...)` — полная страница кричит громко вместо "
        "молчаливой обрезки (AGENTS.md «fail loud»), листать страницы здесь "
        "осознанно не стали.",
    ("scripts/orchestra/scheduler.py", "pr_check_runs"):
        "check-run'ы ОДНОГО PR — фиксированный малый список обязательных "
        "проверок этого репозитория, не растущий список.",
    ("scripts/orchestra/scheduler.py", "merge_queue"):
        "тот же check-runs, что pr_check_runs — тело инлайн внутри merge_queue.",
    ("scripts/orchestra/scheduler.py", "worker_runs_active"):
        "явный `per_page=1` — запрошен только последний прогон, не список.",
    # Появились с #253 (стадия приёмки, слито после этой гвардии, #308/#309) —
    # не находка PR #311, добавлены здесь только чтобы гвардия оставалась
    # зелёной после ребейза на main; классификация та же, что у соседних
    # записей pr_check_runs/merge_queue/recent_runs выше.
    ("scripts/orchestra/scheduler.py", "deploy_evidence"):
        "`per_page=10` — запрошены последние N прогонов ОДНОГО workflow "
        "(deploy-worker.yml), кандидат ищется по head_sha=merge_commit_sha "
        "среди них же — тот же контракт «дай N последних», что у "
        "pulse_guard.recent_runs, не полный список.",
    ("scripts/orchestra/scheduler.py", "script_evidence"):
        "check-runs ОДНОГО коммита (head_sha) — тот же контракт, что "
        "pr_check_runs выше: фиксированный малый список обязательных "
        "проверок этого репозитория, не растущий список.",
}


def _production_scripts() -> list[Path]:
    """Все .py в scripts/, кроме тестов — тесты гоняют собственные фикстуры
    per_page= (например моки в test_scheduler.py), это не то же самое, что
    вызов гвардии в проде."""
    return [
        path for path in (REPO_ROOT / "scripts").rglob("*.py")
        if not path.name.startswith("test_")
    ]


def _enclosing_function(lines: list[str], line_idx: int) -> str | None:
    for idx in range(line_idx, -1, -1):
        match = DEF_RE.match(lines[idx])
        if match:
            return match.group(1)
    return None


def _find_offenders() -> list[str]:
    offenders = []
    for path in _production_scripts():
        rel = str(path.relative_to(REPO_ROOT)).replace("\\", "/")
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        for call in CALL_RE.finditer(text):
            url = call.group(1)
            if PAGE_PARAM_RE.search(url):
                continue  # сам листает страницы — не одностраничный вызов
            line_idx = text.count("\n", 0, call.start())
            func = _enclosing_function(lines, line_idx)
            if (rel, func) in ALLOWED_SINGLE_PAGE_CALLS:
                continue
            offenders.append(f"{rel}:{line_idx + 1} ({func or '<модуль>'}) — {url}")
    return offenders


def test_no_unpaginated_list_calls_outside_allowlist():
    offenders = _find_offenders()
    assert offenders == [], (
        "Сырой одностраничный вызов gh(...per_page=...) без обхода страниц "
        "(класс #308 — тот же, что #294/#303/#276, чинили точечно семь раз): "
        f"{offenders}. Либо обойди страницы (образец: "
        "review_labels.list_pr_files/list_timeline — `&page={page}` в URL и "
        "выход по короткой странице), либо, если список ограничен по природе, "
        "назови причину и добавь запись в ALLOWED_SINGLE_PAGE_CALLS этого файла."
    )


# Мутация, которой доказана гвардия (#308): временно добавь в любой
# производственный файл scripts/ (например в конец этого же файла — тест
# исключает только файлы с именем test_*, но не сам себя как модуль-хелпер,
# поэтому проверяй мутацию во ВРЕМЕННОМ отдельном файле scripts/lib/_tmp_probe.py):
#
#   def raw_call(repo, gh):
#       return gh(f"repos/{repo}/issues?per_page=100")
#
# — этот тест красный (raw_call не в ALLOWED_SINGLE_PAGE_CALLS). Удали
# временный файл — тест снова зелёный.
