"""Единое место правды на создание issue пула программным путём (#526).

Живой случай, из-за которого этот файл появился: issue #523 заведена агентом
напрямую (`gh issue create --label white-spot`), issue #425 — без единой
метки вовсе. Обе обошли шаблон (шаблон применяется только к веб-форме, не к
API/CLI-созданию) и остались без `task` — пул воркера физически их не видит
(`scripts/orchestra/scheduler.py::open_task_issues` читает только
`labels=task`, класс #179). До этого файла четыре программных создателя
(`scripts/review/file_tasks.py`, `scripts/orchestra/scheduler.py::after_merge`
— хвост чеклиста, `scripts/orchestra/stall_detector.py::create_task`,
`scripts/orchestra/upstream_drift.py::create_bump_issue`) сами собирали
`-f labels[]=task` каждый в своём месте — рабочий код по факту, но
без единой точки, которая сделала бы пропуск метки невозможным для СЛЕДУЮЩЕГО
создателя. `create_pool_issue` — эта точка: labels без `task` — RuntimeError
ДО сетевого вызова, не постфактум-гвардия по уже созданной issue.

Канал агентских/ручных запросов (`gh issue create` в терминале, вне
Python-скриптов репозитория) этот файл не покрывает — там единственный
документированный путь — `scripts/gh/issue-create` (тот же инвариант, на
bash). Из cf-worker (Cloudflare Workers, `cf-worker/src/harness.ts`) позвать
ни этот модуль, ни bash-обёртку нельзя — там своя проверка
(`cf-worker/test/harness.spec.ts`, ассерт `labels` содержит `task`).

#720: обязательное машиночитаемое объявление связи в теле issue (второй
инвариант рядом с меткой `task`, тот же приём — отказ ДО сетевого вызова).
Распознавание здесь НЕ дублируется: гейт задаёт уже существующему
объединённому читателю `declared_deps.declared_blocked_by` ровно один
вопрос — «объявление в теле есть?» (`None` — ни структурного поля «Чем
блокируется», ни инлайн-строки «БЛОКИРУЕТСЯ: …»). Это тот же предикат, что
читают потребители графа (`auto_wire`, `repo_invariants`), — гейт по
построению пропускает только тела, объявление в которых уже разбирается
существующими местами правды; ЛЮБОЙ ответ валиден (пустой, «ничем», номера,
неразобранный текст — поле обязательное, а не непустое, а качество ответа —
дело `auto_wire`/`declared_deps check`, не гейта заведения). Тот же вопрос
для bash-обёртки — подкоманда `check-body` ниже: один парсер на bash и
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

    Ответ даёт существующий объединённый читатель
    `declared_deps.declared_blocked_by` (структурное поле «Чем блокируется»
    через `form_field_numbers` + инлайн-строка «БЛОКИРУЕТСЯ: …» последней
    непустой строкой через `ai_review.blocked_by_numbers`) — тот же, что
    читают потребители графа. `None` — ни одной формы; не-`None` — форма
    есть (`[]` — явное «ничем», список — номера, `UNRECOGNIZED_FORM` —
    заполненное поле с неразобранным ответом: данные на входе есть,
    разбор отдельного качества — дело читателей графа, не этого гейта).
    """
    if not body:
        return False
    return declared_deps.declared_blocked_by(body) is not None


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
            f"объявления связи (поле «Чем блокируется» или строка "
            f"«БЛОКИРУЕТСЯ: …») — задача #720: 89% пула без связи, уровень "
            f"приоритета не работает. Добавь в тело:\n{hint}"
        )
    args = ["-X", "POST", f"repos/{repo}/issues", "-f", f"title={title}", "-f", "body=" + body]
    for label in labels:
        args += ["-f", f"labels[]={label}"]
    return gh(*args)


def _main(argv: list[str]) -> int:
    """CLI для bash-обёртки `scripts/gh/issue-create` (#720):
    `pool_issue.py check-body < тело` — rc 0, если объявление есть;
    rc 1 и газ (готовая строка) в stderr, если нет. Тело читается из stdin
    БАЙТАМИ с явным декодированием — кодировка по умолчанию платформы здесь
    не участвует (тот же класс #723).
    """
    if argv[1:2] != ["check-body"]:
        print("использование: pool_issue.py check-body < body", file=sys.stderr)
        return 2
    body = sys.stdin.buffer.read().decode("utf-8")
    if _has_declared_dependency(body):
        return 0
    print(
        "тело issue не содержит машиночитаемого объявления связи "
        "(поле «Чем блокируется» или строка «БЛОКИРУЕТСЯ: …»); "
        "ответ «ничем» валиден, отсутствие обеих форм — нет. Добавь в тело:",
        file=sys.stderr,
    )
    print(_dependency_error_hint(), file=sys.stderr, end="")
    return 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
