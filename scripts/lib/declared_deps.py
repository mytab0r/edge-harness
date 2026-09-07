#!/usr/bin/env python3
"""Обнаружение рассинхрона «тело задачи называет блокирующую issue — нативный
граф её не знает» (задача #371, продолжение #361/task_deps.py).

`find_desync` — только детектор для человека/CI, сам по себе НЕ источник
истины и НИЧЕГО не пишет в граф (о записи модулем в граф — `auto_wire`
ниже), эвристика по формулировкам прозы («после #N», «зависит от
#N», «блокируется #N», «заблокирован(а) #N», «блокер — #N», «до закрытия
#N»). Ложные срабатывания здесь ожидаемы (та же формулировка встречается не
только для реальной межзадачной зависимости — «после слияния» про порядок
шагов внутри одной задачи, не про #N) — именно поэтому ЭТА находка не
пишет связь автоматически (в отличие от структурного переноса —
`file_tasks.py::wire_declared_dependency` для строки «БЛОКИРУЕТСЯ: …» и
`auto_wire` ниже для поля формы, переносящих СТРУКТУРНЫЙ ответ, не любое
упоминание в прозе) и не проваливает обязательный CI-гейт —
только видимое предупреждение (`::warning::`), см. `.github/workflows/
repo-ci.yml`. Живой замер при внедрении (2026-09-06): более широкий захват
(окно `\\D{0,20}` без исключения `)`/`.`) давал ложный матч «Поглощает часть
#227 (закрытие задач после слияния) и часть #201» — триггер «после» из
несвязанной скобки цеплял #201 через закрывающую скобку. Окно сужено до
`[^#).\\n]{0,20}` (не пересекает конец скобки/предложения) — тот же прогон
после сужения даёт 0 таких ложных матчей на реальном пуле репозитория.

Ссылка на уже ЗАКРЫТУЮ/несуществующую/не-task issue — НЕ рассинхрон:
приоритет читает только ОТКРЫТЫХ блокирующих (`blocked_by_open`), отсутствие
связи на закрытую задачу не влияет ни на что (design.md task-priority-
blocking-graph, задача #371 report).

Отдельно — `auto_wire`/CLI `wire` (задача #529, продолжение #371): значение
СТРУКТУРНОГО поля формы «Чем блокируется» (`form_field_numbers`, тот же
`_FORM_FIELD_RE`, что и `declared_candidates` выше) — не эвристика, ответ
схемы issue-формы, поэтому переносится в граф АВТОМАТИЧЕСКИ, тем же путём,
что `file_tasks.py::wire_declared_dependency` уже делает для строки
«БЛОКИРУЕТСЯ:» (общий цикл — `task_deps.wire_dependencies`). Прозаическая
эвристика `_TRIGGER_RE` в автопереносе НЕ участвует ни в каком виде — только
в `find_desync` (предупреждение, не запись).

CLI:
    python scripts/lib/declared_deps.py check <owner/repo> [label]
    python scripts/lib/declared_deps.py wire <owner/repo> [label]
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

_TRIGGER_RE = re.compile(
    r"(?:после|зависит\s+от|блокируется|заблокирован[а-я]*|блокер|до\s+закрытия)"
    r"[^#).\n]{0,20}#(\d+)",
    re.IGNORECASE,
)

# Канонический рендер обязательного поля формы «Чем блокируется»
# (.github/ISSUE_TEMPLATE/task.yml, white-spot.yml) — GitHub рендерит поле
# формы как `### <label>\n\n<ответ>\n` (design.md task-priority-blocking-graph,
# развилка про рендер формы). Это форма СХЕМЫ, не прозопарсинг: значение поля
# приходит структурно, ответ — первая непустая строка после заголовка.
# История: находка AI-ревью PR #387 добавила этот рендер в ЭВРИСТИКУ
# (declared_candidates ниже), потому что перенос поля в граф был ручной и
# пропуск был наиболее вероятен. С #529 перенос СТРУКТУРНОГО поля
# автоматический (auto_wire ниже), и включать его в эвристику — значит
# предупреждать о том, что тот же прогон тут же чинит (находка AI-ревью
# PR #537): эвристика ниже снова чисто прозовая, поле читает только
# `form_field_numbers` — детерминированный путь.
_FORM_FIELD_RE = re.compile(
    r"^###\s*Чем\s+блокируется\s*\n\s*\n([^\n]*)",
    re.IGNORECASE | re.MULTILINE,
)


def declared_candidates(body: str) -> set[int]:
    """Номера issue рядом с формулировкой зависимости — эвристика для
    предупреждения, не факт (см. docstring модуля). Структурное поле формы
    «Чем блокируется» сюда НЕ входит с #529: его переносит детерминированный
    `auto_wire`, и предупреждение на него было бы шумом о том, что тот же
    прогон чинит (см. комментарий у `_FORM_FIELD_RE` выше)."""
    return {int(n) for n in _TRIGGER_RE.findall(body or "")}


def form_field_numbers(body: str) -> list[int] | None:
    """Структурный ответ обязательного поля «Чем блокируется» (issue-формы
    `task.yml`/`white-spot.yml`, обязательное с #387) — не эвристика прозы,
    ответ схемы формы: GitHub рендерит поле как `### Чем блокируется\\n\\n<ответ>`,
    ответ — первая непустая строка после заголовка (`_FORM_FIELD_RE`).

    `None` — секции нет вовсе в теле (issue не из этого шаблона, или заведена
    до введения обязательного поля) — автопереносу не из чего переносить,
    это НЕ то же самое, что «явно ничем» (см. ниже).
    `[]` — явный ответ «ничем» (или пустое значение) — зависимостей нет,
    ВАЛИДНЫЙ ответ обязательного поля, не повод для предупреждения/шума
    (задача #529, требование «поле обязательное — «ничем» тоже ответ»).
    Иначе — список номеров issue, названных в ответе."""
    match = _FORM_FIELD_RE.search(body or "")
    if not match:
        return None
    value = match.group(1).strip()
    if not value or value.lower() == "ничем":
        return []
    return [int(n) for n in re.findall(r"#(\d+)", value)]


def auto_wire(
    repo: str, label: str = "task", gh_call=None, log=print,
) -> list[dict]:
    """Единый механизм автопереноса структурного поля формы «Чем блокируется»
    в нативный `blockedBy` — задача #529, продолжение #371/#387: поле
    обязательно с #387, но до этой задачи перенос для человеческого/шаблонного
    пути был ручной командой `task_deps.py block`. Переиспользует
    `task_deps.wire_dependencies` (тот же цикл, что уже применяет
    `file_tasks.py::wire_declared_dependency` для АИ-ревью-пути) — не третья
    копия «пропустить номер вне пула, иначе add_dependency».

    Идемпотентно: номера, уже присутствующие в `blocked_by_open`, повторно не
    линкуются (не тратит сетевой вызов на уже поставленную связь) — безопасно
    гонять на каждый push, не только один раз. Номер вне открытого пула
    (закрыт/не задача/не существует) и ссылка на саму issue отфильтровываются
    ещё здесь, без предупреждения: этот путь — периодический, предупреждение
    на каждом push превратилось бы в постоянный шум, а связь на закрытую
    «не влияет ни на что» (см. docstring модуля; find_desync тот же случай
    молчит). Одноразовые пути (АИ-ревью) получают предупреждение от самого
    `task_deps.wire_dependencies`.

    Возвращает список `{"issue": N, "linked": [...]}` только для issues, где
    реально что-то дописано в граф на ЭТОМ прогоне."""
    task_deps = _load_task_deps()
    gh_call = gh_call or task_deps._default_gh
    issues = task_deps.fetch_pool(repo, label=label, include_body=True, gh_call=gh_call)
    open_numbers = {issue["number"] for issue in issues}
    report: list[dict] = []
    for issue in issues:
        number = issue["number"]
        numbers = form_field_numbers(issue.get("body") or "")
        if not numbers:
            continue  # None (поля нет) или [] (явно «ничем») — переносить нечего
        already = set(issue.get("blocked_by_open") or [])
        # Закрытый/внепуловый номер и ссылка на себя отфильтровываются ЗДЕСЬ,
        # до делегирования в wire_dependencies: связь на закрытую «не влияет
        # ни на что» (docstring модуля выше; find_desync тот же случай молчит
        # намеренно), а этот обход бежит на КАЖДЫЙ push в main — передать
        # такой номер дальше значило бы заново выяснять одно и то же и
        # печатать одно и то же предупреждение на каждом прогоне, без
        # ограничения частоты (находка AI-ревью PR #537). Предупреждение
        # остаётся в wire_dependencies для одноразовых путей (АИ-ревью).
        missing = [n for n in numbers
                   if n not in already and n in open_numbers and n != number]
        if not missing:
            continue  # уже в графе (идемпотентность) либо номера неприменимы
        linked = task_deps.wire_dependencies(
            repo, number, missing, open_numbers, gh_call=gh_call, log=log)
        if linked:
            report.append({"issue": number, "linked": linked})
    return report


def find_desync(issues: list[dict]) -> list[dict]:
    """`issues` — форма `task_deps.fetch_pool(..., include_body=True)`:
    ключи `number`, `body`, `blocked_by_open`. Возвращает находки
    `{"issue": N, "declared_blocking": M}` — тело N называет M формулировкой
    зависимости, M сам открыт и есть в пуле, но нативного `blockedBy` на M
    у issue N нет. Пусто — рассинхрона не обнаружено (легитимный, ожидаемый
    исход большую часть времени: тело либо совпадает с графом, либо ссылка
    на что-то, что не является межзадачной зависимостью вовсе)."""
    open_numbers = {issue["number"] for issue in issues}
    findings: list[dict] = []
    for issue in issues:
        number = issue["number"]
        declared = declared_candidates(issue.get("body") or "")
        native = set(issue.get("blocked_by_open") or [])
        missing = {n for n in declared if n in open_numbers and n != number} - native
        for n in sorted(missing):
            findings.append({"issue": number, "declared_blocking": n})
    return findings


def _load_task_deps():
    spec = importlib.util.spec_from_file_location(
        "task_deps", Path(__file__).resolve().with_name("task_deps.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def main(argv: list[str]) -> int:
    if len(argv) in (2, 3) and argv[0] == "check":
        task_deps = _load_task_deps()
        repo = argv[1]
        label = argv[2] if len(argv) == 3 else "task"
        issues = task_deps.fetch_pool(repo, label=label, include_body=True)
        findings = find_desync(issues)
        if not findings:
            print("declared_deps: рассинхрона не найдено")
            return 0
        for finding in findings:
            print(
                f"::warning::declared_deps: #{finding['issue']} называет в теле "
                f"формулировкой зависимости открытую #{finding['declared_blocking']}, "
                f"но нативного blockedBy на неё нет — проверь и при подтверждении "
                f"поставь `python scripts/lib/task_deps.py block {repo} "
                f"{finding['issue']} {finding['declared_blocking']}`"
            )
        return 0
    if len(argv) in (2, 3) and argv[0] == "wire":
        repo = argv[1]
        label = argv[2] if len(argv) == 3 else "task"
        report = auto_wire(repo, label)
        if not report:
            print("declared_deps: переносить нечего (поле пусто/«ничем»/уже в графе)")
            return 0
        for item in report:
            linked = " ".join(f"#{n}" for n in item["linked"])
            print(f"declared_deps: #{item['issue']} -> {linked} (поле «Чем блокируется»)")
        return 0
    print(
        "использование: declared_deps.py check <owner/repo> [label] "
        "| wire <owner/repo> [label]",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
