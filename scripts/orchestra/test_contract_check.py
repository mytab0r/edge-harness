#!/usr/bin/env python3
"""Гвардия класса «нарушение записано → изменяющий вызов всё равно выполнен».

Живой случай (лог прогона 2026-09-06, контракт PR #359):

    contract: авто-назначение mytab0r на #131
    ##[error]Задача #131 закрыта — возьми открытую или заведи новую.
    contract: FAIL (1 нарушений)

Контракт сначала НАЗНАЧИЛ владельца на закрытую задачу, потом сказал, что
задачу брать нельзя — назначение уже никто не отменит автоматом. Причина:
проверка `issue["state"] != "open"` только копила строку в `problems`, а
авто-назначение (`gh -X POST .../assignees`) стояло в той же ветке и не
смотрело на `problems` вообще.

Фикс — task_eligibility_problems(): один список причин непригодности задачи
(не issue/закрыта/нет метки task/blocked), который main() обязан проверить
ДО единого изменяющего вызова над issue_number. Тесты ниже доказывают это на
живой прод-форме (закрытая issue, задача без метки task, задача blocked):
считают изменяющие вызовы (FakeGh.mutating_calls) и требуют, чтобы среди них
не было ни одного `.../assignees` — что бы ни случилось внутри main().

Мутационная проверка (см. docstring test_closed_task_gets_no_assignment_call):
откатить фикс — тест краснеет.

Запуск: python -m pytest scripts/orchestra/test_contract_check.py -q
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parent
SCRIPT = _DIR / "contract_check.py"
_spec = importlib.util.spec_from_file_location("contract_check", SCRIPT)
cc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cc)  # type: ignore[union-attr]

REPO = "mytab0r/edge-harness"


def label(name):
    return {"id": "LA_kwDOUHBaqc8AAAACypPLSQ", "name": name}


def pull(number, *, pr_body="", labels=(), author="mytab0r", branch=None):
    # Параметр называется pr_body, не body (тот же приём, что test_scheduler.py::pull):
    # CI-гвардия класса #124 (repo-ci.yml "Оркестрация без keyword-аргументов gh()")
    # грепает вызовы вида "запятая-пробел-body-равно" по всему scripts/orchestra/
    # буквально, без разбора AST — keyword-параметр тестового хелпера с таким же
    # именем ловится тем же паттерном, что и настоящий баг (позиционный
    # "-f", f"body=..." — единственная разрешённая форма для gh()).
    # `branch` не задан по умолчанию (не "" ) — большинство тестов этого файла
    # доказывают поведение декларации тела БЕЗ ветки agent/<N>-slug, и головы
    # без поля `head` вовсе (как в прод-форме `gh api .../pulls/N`, где ветка
    # реального PR почти всегда есть) — тест не должен молчаливо считать это
    # эквивалентным "ветка есть, но пустая".
    result = {
        "number": number,
        "body": pr_body,
        "labels": [label(n) for n in labels],
        "user": {"login": author},
    }
    if branch is not None:
        result["head"] = {"ref": branch}
    return result


def issue(number, *, state="open", labels=("task",), assignees=()):
    return {
        "number": number,
        "state": state,
        "labels": [label(n) for n in labels],
        "assignees": [{"login": a} for a in assignees],
    }


class FakeGh:
    """Маршрутизатор вызовов gh api по подстроке пути; каждый вызов
    записывается — доказательство «ни одного изменяющего вызова» строится на
    этом списке, а не на пересказе (тот же приём, что и в test_scheduler.py)."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, *args):
        joined = " ".join(args)
        self.calls.append(joined)
        for fragment, result in self.routes.items():
            if fragment in joined:
                if isinstance(result, Exception):
                    raise result
                return result
        raise AssertionError(f"нет маршрута для: {joined}")

    def mutating_calls(self):
        return [c for c in self.calls if c.startswith(("-X POST", "-X PUT", "-X DELETE", "-X PATCH"))]


def _run_main(monkeypatch, fake, *, pr_number):
    """Прогоняет main() с подставным gh/run_gh и argv. Провал (fail())
    завершает процесс через sys.exit(1) — успех main() просто возвращает 0,
    ничего не бросая; оба исхода нормализуются в код возврата."""
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setattr(cc, "gh", fake)
    monkeypatch.setattr(cc, "run_gh", lambda *args: None)  # метки/комментарий провала — не предмет теста
    monkeypatch.setattr(sys, "argv", ["contract_check.py", "--pr", str(pr_number)])
    try:
        return cc.main()
    except SystemExit as exit_:
        return exit_.code


# ── Прод-форма живого случая: закрытая задача, PR её объявляет ──────────────


def test_closed_task_gets_no_assignment_call(monkeypatch):
    """Живая форма PR #359 (#131 закрыта): PR объявляет закрытую задачу без
    исполнителя. Контракт обязан провалиться БЕЗ единого изменяющего вызова
    над issue_number — назначать некого на закрытую задачу.

    Мутация: замени `eligibility = task_eligibility_problems(...)` +
    `if eligibility: ... else: ...` обратно на исходную форму (проверка state
    только добавляет в problems, авто-назначение в той же ветке без условия
    на eligibility) — этот тест краснеет с AssertionError на непустом
    assignees-вызове."""
    routes = {
        "pulls/359": pull(359, pr_body="#131\n\nостальной текст", labels=()),
        "issues/131": issue(131, state="closed", assignees=()),
    }
    fake = FakeGh(routes)
    code = _run_main(monkeypatch, fake, pr_number=359)

    assert code == 1, "контракт обязан провалиться на закрытой задаче"
    assignment_calls = [c for c in fake.mutating_calls() if "assignees" in c]
    assert not assignment_calls, (
        "назначение на закрытую задачу выполнилось — тот самый живой баг "
        f"(лог 2026-09-06, PR #359): {assignment_calls}"
    )


def test_no_task_label_gets_no_assignment_call(monkeypatch):
    """Тот же класс, вторая причина непригодности: issue есть, открыта, но
    без метки `task`. Авто-назначение не должно выполняться и здесь —
    непригодность есть непригодность, независимо от конкретной причины."""
    routes = {
        "pulls/400": pull(400, pr_body="#500\n\nтекст", labels=()),
        "issues/500": issue(500, state="open", labels=(), assignees=()),
    }
    fake = FakeGh(routes)
    code = _run_main(monkeypatch, fake, pr_number=400)

    assert code == 1
    assignment_calls = [c for c in fake.mutating_calls() if "assignees" in c]
    assert not assignment_calls, f"назначение на задачу без метки task: {assignment_calls}"


def test_blocked_task_gets_no_assignment_call(monkeypatch):
    """Третья причина непригодности: задача открыта, с меткой task, но
    заблокирована владельцем (`blocked`, docs/agents/LABELS.md) — эскалация
    playbook держит существующее назначение, но НОВОЕ авто-назначение через
    контракт не должно случиться."""
    routes = {
        "pulls/401": pull(401, pr_body="#501\n\nтекст", labels=()),
        "issues/501": issue(501, state="open", labels=("task", "blocked"), assignees=()),
    }
    fake = FakeGh(routes)
    code = _run_main(monkeypatch, fake, pr_number=401)

    assert code == 1
    assignment_calls = [c for c in fake.mutating_calls() if "assignees" in c]
    assert not assignment_calls, f"назначение на заблокированную задачу: {assignment_calls}"


# ── Контроль: пригодная свободная задача по-прежнему авто-назначается ───────


def test_eligible_free_task_still_gets_auto_assigned(monkeypatch):
    """Контроль (иначе тесты выше ничего бы не доказывали сами по себе):
    открытая задача с меткой task и без исполнителя — легитимный путь
    авто-назначения (docstring contract_check.py, правило 4) обязан
    сохраниться."""
    routes = {
        "pulls/600": pull(600, pr_body="#700\n\nтекст", labels=(), author="mytab0r"),
        "issues/700": issue(700, state="open", labels=("task",), assignees=()),
        "pulls?state=open": [],
    }
    fake = FakeGh(routes)
    code = _run_main(monkeypatch, fake, pr_number=600)

    assert code == 0, "пригодная свободная задача обязана пройти контракт"
    assignment_calls = [c for c in fake.mutating_calls() if "assignees" in c]
    assert len(assignment_calls) == 1
    assert "assignees[]=mytab0r" in assignment_calls[0]


# ── Чистая функция task_eligibility_problems напрямую ───────────────────────


def test_task_eligibility_problems_pull_request_short_circuits():
    problems = cc.task_eligibility_problems({"pull_request": {}}, 42)
    assert problems == ["#42 — это PR, а не задача из пула."]


def test_task_eligibility_problems_open_task_labeled_not_blocked_is_eligible():
    problems = cc.task_eligibility_problems(issue(1, state="open", labels=("task",)), 1)
    assert problems == []


def test_task_eligibility_problems_collects_multiple_reasons():
    # Закрыта И без метки task одновременно — обе причины обязаны попасть в
    # список (не «первая найденная и остановились»): main() докладывает все.
    problems = cc.task_eligibility_problems(issue(1, state="closed", labels=()), 1)
    assert len(problems) == 2


# ── #394: задача PR резолвится по ветке, не по прямому declared_tasks ───────


def test_task_number_taken_from_branch_when_body_prose_first_line(monkeypatch):
    """Ветка agent/<N>-slug первична: тело PR не начинается голым #N (живой
    случай #388 из постановки #394 — первая строка была прозой «Задача #256
    (task-rework-loop). Реализует пп.1-2», контракт красил «нет ссылки на
    задачу», хотя ветка и так называет её однозначно). Задача из ветки
    пригодна и свободна — контракт обязан пройти и назначить автора именно
    на неё, а не упасть из-за формы первой строки тела."""
    routes = {
        "pulls/900": pull(
            900, branch="agent/256-task-rework-loop",
            pr_body="Задача #256 (task-rework-loop). Реализует пп.1-2",
        ),
        "issues/256": issue(256, state="open", assignees=()),
        "pulls?state=open": [],
    }
    fake = FakeGh(routes)
    code = _run_main(monkeypatch, fake, pr_number=900)

    assert code == 0, "ветка однозначно называет открытую задачу — контракт обязан пройти"
    assignment_calls = [c for c in fake.mutating_calls() if "assignees" in c]
    assert len(assignment_calls) == 1
    assert "issues/256/assignees" in assignment_calls[0]


def test_no_branch_and_no_declaration_names_the_fix(monkeypatch):
    """Отказ обязан называть готовое действие (постановка #394, п.4), не
    просто «нет ссылки на задачу»."""
    routes = {
        "pulls/901": pull(901, pr_body="просто описание без номера задачи"),
    }
    fake = FakeGh(routes)
    code = _run_main(monkeypatch, fake, pr_number=901)

    assert code == 1
    assert not fake.mutating_calls()


def test_rework_supersession_body_overrides_closed_branch_task(monkeypatch):
    """Живой класс #394 (4 из 30 открытых PR репозитория на 2026-09-06: #388,
    #384, #359, #167): задача закрыта раньше срока («закрытая задача не
    переоткрывается»), докрытие оформлено НОВОЙ узкой задачей, объявленной
    первой строкой тела — ветку переименовать нельзя (agent/<N>-slug ставится
    один раз). Контракт обязан подхватить пригодную задачу из тела, а не
    упасть на закрытой из ветки."""
    routes = {
        "pulls/902": pull(
            902, branch="agent/256-task-rework-loop",
            pr_body="#391\n\nRelated: #256 (закрыта акцептансом, докрытие — #391)",
        ),
        "issues/256": issue(256, state="closed", assignees=("mytab0r",)),
        "issues/391": issue(391, state="open", assignees=()),
        "pulls?state=open": [],
    }
    fake = FakeGh(routes)
    code = _run_main(monkeypatch, fake, pr_number=902)

    assert code == 0, "тело объявляет пригодную задачу-преемницу — контракт обязан пройти"
    assignment_calls = [c for c in fake.mutating_calls() if "assignees" in c]
    assert len(assignment_calls) == 1
    assert "issues/391/assignees" in assignment_calls[0], (
        "контракт обязан назначить на ОТКРЫТУЮ задачу-преемницу #391, "
        f"не на закрытую #256 из ветки: {assignment_calls}"
    )


def test_both_branch_and_body_task_closed_reports_branch_reason(monkeypatch):
    """Ни один кандидат не пригоден — контракт докладывает причину ПЕРВОГО
    (ветка, надёжный источник), не молчит и не выбирает произвольно."""
    routes = {
        "pulls/903": pull(
            903, branch="agent/256-task-rework-loop",
            pr_body="#392\n\nтекст",
        ),
        "issues/256": issue(256, state="closed"),
        "issues/392": issue(392, state="closed"),
    }
    fake = FakeGh(routes)
    code = _run_main(monkeypatch, fake, pr_number=903)

    assert code == 1
    assert not fake.mutating_calls()


def test_duplicate_pr_detected_via_other_branch_not_only_declaration(monkeypatch):
    """Симметрия #394: чужой открытый PR на ту же задачу ловится и по имени
    его ветки, даже если его тело не декларирует номер первой строкой (PR по
    шаблону, автор ещё не заполнил описание) — раньше (`declares_task` по
    телу) такой конфликт был бы не пойман вовсе."""
    routes = {
        "pulls/906": pull(906, branch="agent/701-race", pr_body="#701\n\nтекст"),
        "issues/701": issue(701, state="open", assignees=()),
        "pulls?state=open": [
            pull(907, branch="agent/701-race-again", pr_body="шаблонное описание без декларации"),
        ],
    }
    fake = FakeGh(routes)
    code = _run_main(monkeypatch, fake, pr_number=906)

    assert code == 1, (
        "чужой открытый PR #907 назвал ту же задачу #701 своей веткой — "
        "второй PR на задачу не проходит контракт, даже без декларации в теле"
    )
