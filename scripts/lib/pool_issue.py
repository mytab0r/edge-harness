"""Единое место правды на создание issue пула программным путём (#526).

Живой случай, из-за которого этот файл появился: issue #523 заведена агентом
напрямую (`gh issue create --label white-spot`), issue #425 — без единой
метки вовсе. Обе обошли шаблон (шаблон применяется только к веб-форме, не к
API/CLI-созданию) и остались без `task` — пул воркера физически их не видит
(`scripts/orchestra/scheduler.py::open_task_issues` читает только
`labels=task`, класс #179). До этого файла первые четыре программных
создателя (`scripts/review/file_tasks.py`, `scripts/orchestra/scheduler.py`
— тогда ещё живой хвост чеклиста `after_merge`, #462,
`scripts/orchestra/stall_detector.py::create_task`,
`scripts/orchestra/upstream_drift.py::create_bump_issue`) сами собирали
`-f labels[]=task` каждый в своём месте — рабочий код по факту, но
без единой точки, которая сделала бы пропуск метки невозможным для СЛЕДУЮЩЕГО
создателя. Живые создатели на сейчас (замер PR #804, раунд 4): восемь через
этот гейт — `file_tasks.py`, `stall_detector.py::create_task`,
`upstream_drift.py::create_bump_issue`,
`scheduler.py::_create_task_replacement`, `pulse_guard.py::failure_watch`,
`dependabot_alert_watch.py`, `health_audit.py`, `merge_health_watch.py` —
и один через bash-обёртку ниже (`scripts/measure/quota_alert.py`).
`create_pool_issue` — эта точка: labels без `task` — RuntimeError
ДО сетевого вызова, не постфактум-гвардия по уже созданной issue.

Канал агентских/ручных запросов (`gh issue create` в терминале, вне
Python-скриптов репозитория) этот файл не покрывает — там единственный
документированный путь — `scripts/gh/issue-create` (тот же инвариант, на
bash). Инбокс морды (cf-worker → repository_dispatch → job в
`.github/workflows/inbox-issue.yml`) зовёт ни то, ни другое — сырой
`gh api`; его шаг дописывает недостающее объявление тем же парсером
(`check-body`) прямо в job'е, само заведение при этом НЕ блокируется:
директива владельца не падает на форме записи.

#720: обязательное машиночитаемое объявление связи в теле issue (второй
инвариант рядом с меткой `task`, тот же приём — отказ ДО сетевого вызова).
Распознавание здесь НЕ дублируется: гейт задаёт уже существующим
объединённым читателям `declared_deps` ровно один вопрос — «объявление в
теле есть?» — и отвечает на него ТЕМ ЖЕ предикатом, каким читают
потребители графа (`auto_wire`, `repo_invariants`): `declared_blocked_by`
(структурное поле «Чем блокируется» + инлайн-строка «БЛОКИРУЕТСЯ: …»)
или `blocking_field_numbers` (обратное поле «Что блокирует», задача
#710 — `auto_wire` переносит его с разворотом направления, поэтому для
гейта равноправно прямому). Гейт по построению пропускает только тела,
объявление в которых уже разбирается существующими местами правды; ЛЮБОЙ
ответ валиден (пустой, «ничем», номера, неразобранный текст — поле
обязательное, а не непустое, а качество ответа — дело
`auto_wire`/`declared_deps check`, не гейта заведения). Тот же вопрос для
bash-обёртки — подкоманда `check-body` ниже: один парсер на bash и
Python, не копия регэкспа (awk-копия первого захода PR #804 разошлась с
Python-гейтом на ответе из одних пробелов — находка AI-ревью, класс «три
копии проверки» закрыт делегацией).
"""
from __future__ import annotations

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import sys
from typing import Callable, Sequence

GhFn = Callable[..., dict | list | None]

REQUIRED_LABEL = "task"

# Место правды на РАСПОЗНАВАНИЕ объявления связи (#720) — загружается по
# файлу (тот же importlib-паттерн, что у всех соседей). Только declared_deps,
# НЕ ai_review напрямую: ai_review на верхнем уровне грузит pulse_guard,
# а тот — этот файл (живой RecursionError при первом прогоне) — цикл
# разрывает сам declared_deps, лениво грузя ai_review внутри
# `declared_blocked_by` (уже на вызове, см. `_load_ai_review` там).
_LIB_DIR = Path(__file__).resolve().parent
_dd_spec = importlib.util.spec_from_file_location("declared_deps", _LIB_DIR / "declared_deps.py")
declared_deps = importlib.util.module_from_spec(_dd_spec)
_dd_spec.loader.exec_module(declared_deps)


def _has_declared_dependency(body: str) -> bool:
    """Тело несёт машиночитаемое объявление связи (#720)?

    Ответ дают существующие объединённые читатели declared_deps — тот же
    набор, что читают потребители графа: `declared_blocked_by`
    (структурное поле «Чем блокируется» через `form_field_numbers` +
    инлайн-строка «БЛОКИРУЕТСЯ: …» последней непустой строкой через
    `ai_review.blocked_by_numbers`) ИЛИ `blocking_field_numbers`
    (обратное поле «Что блокирует», #710: если A отвечает «Что блокирует:
    B», граф ставит B blockedBy A — тело с этим полем уже несёт
    разбираемую связь, отказывать ему значило бы требовать второй записи
    того же факта). `None` — ни одной формы; не-`None` — форма
    есть (`[]` — явное «ничем», список — номера, `UNRECOGNIZED_FORM` —
    заполненное поле с неразобранным ответом: данные на входе есть,
    разбор отдельного качества — дело читателей графа, не этого гейта).
    """
    if not body:
        return False
    if declared_deps.declared_blocked_by(body) is not None:
        return True
    return declared_deps.blocking_field_numbers(body) is not None


def _append_note(body: str, note: str) -> str:
    """Дописывает `note` в конец тела, НЕ разрушая объявление связи (#720).

    Инлайн-форма «БЛОКИРУЕТСЯ: …» живёт ПОСЛЕДНЕЙ непустой строкой тела
    (`ai_review.blocked_by_numbers`), поэтому любая приписка после неё
    убивает объявление на выходе: находимка AI-ревью PR #804 — футер
    `--confirm-not-duplicate` обёртки дописывался к уже проверенному телу,
    и созданная issue объявление больше не несла, хотя гейт его требовал.
    Если приписка ломает предикат — последняя непустая строка ИСХОДНОГО
    тела переносится в конец ЗА припиской ДОСЛОВНО, без нового разбора её
    формата: решает тот же `_has_declared_dependency`, строку читает тот
    же `ai_review.blocked_by_numbers`, третьего парсера нет. Тело без
    объявления не чинится (гейт заведения выше его и так не пропустит) и
    телом со структурным полем не трогается вовсе — заголовок с ответом
    положению последней строки не обязан.
    """
    candidate = body + note
    if _has_declared_dependency(body) and not _has_declared_dependency(candidate):
        lines = [line for line in body.splitlines() if line.strip()]
        if lines:
            candidate = candidate + "\n" + lines[-1]
    return candidate


def _dependency_error_hint() -> str:
    """Готовая строка для вставки в тело — газ к тормозу."""
    return (
        "### Чем блокируется\n"
        "ничем\n"
    )


def create_pool_issue(
    gh: GhFn,
    repo: str,
    title: str,
    body: str,
    labels: Sequence[str],
) -> dict:
    """Заводит issue POST'ом `repos/{repo}/issues` через переданный `gh`
    (тот же `gh(*args)`, что уже используют file_tasks.py/scheduler.py/
    stall_detector.py — подключение по инъекции зависимости, не импорт
    транспорта: у каждого вызывающего свой модуль gh() поверх `gh api`,
    второй копии транспорта здесь не заводим).

    Отказывает ДО вызова gh, если среди labels нет `task` — issue не
    создаётся, сеть не тратится (тот же приём, что у `scripts/git/pr-create`
    и `scripts/gh/issue-create`: проверка на входе, не гвардия по факту).

    #720: Отказывает ДО вызова gh, если тело не несет машиночитаемого
    объявления связи (см. `_has_declared_dependency`). Ответ «ничем» валиден.
    В тексте отказа — готовая строка для вставки.
    """
    if REQUIRED_LABEL not in labels:
        raise RuntimeError(
            f"create_pool_issue: labels={list(labels)} без обязательной "
            f"«{REQUIRED_LABEL}» — issue не видна пулу воркера "
            f"(класс #179, живой случай #523/#425), не завожу."
        )
    if not _has_declared_dependency(body):
        hint = _dependency_error_hint()
        raise RuntimeError(
            f"create_pool_issue: тело issue не содержит машиночитаемого "
            f"объявления связи (поле «Чем блокируется», обратное поле "
            f"«Что блокирует» или строка «БЛОКИРУЕТСЯ: …») — задача #720: "
            f"89% пула без связи, уровень приоритета не работает. "
            f"Добавь в тело:\n{hint}"
        )
    args = ["-X", "POST", f"repos/{repo}/issues", "-f", f"title={title}", "-f", "body=" + body]
    for label in labels:
        args += ["-f", f"labels[]={label}"]
    return gh(*args)


def _main(argv: list[str]) -> int:
    """CLI для bash-обёртки `scripts/gh/issue-create` и job'а
    `inbox-issue.yml` (#720):

    `pool_issue.py check-body < тело` — rc 0, если объявление есть;
    rc 1 и газ (готовая строка) в stderr, если нет.

    `pool_issue.py append-note <файл> < тело > тело-с-припиской` —
    дописывает содержимое файла в конец тела через `_append_note`
    (инлайн-объявление остаётся последней непустой строкой).

    Тело читается из stdin БАЙТАМИ с явным декодированием — кодировка по
    умолчанию платформы здесь не участвует (тот же класс #723).
    """
    if argv[1:2] == ["check-body"]:
        body = sys.stdin.buffer.read().decode("utf-8")
        if _has_declared_dependency(body):
            return 0
        print(
            "тело issue не содержит машиночитаемого объявления связи "
            "(поле «Чем блокируется», обратное поле «Что блокирует» или "
            "строка «БЛОКИРУЕТСЯ: …»); "
            "ответ «ничем» валиден, отсутствие всех форм — нет. Добавь в тело:",
            file=sys.stderr,
        )
        print(_dependency_error_hint(), file=sys.stderr, end="")
        return 1
    if argv[1:2] == ["append-note"]:
        if len(argv) < 3:
            print("использование: pool_issue.py append-note <файл> < body", file=sys.stderr)
            return 2
        body = sys.stdin.buffer.read().decode("utf-8")
        note = Path(argv[2]).read_text(encoding="utf-8")
        sys.stdout.write(_append_note(body, note))
        return 0
    print(
        "использование: pool_issue.py check-body < body\n"
        "               pool_issue.py append-note <файл> < body",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
