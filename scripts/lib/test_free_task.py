#!/usr/bin/env python3
"""Тесты выбора свободной задачи (scripts/lib/free_task.py, #245).

Два дефекта `free_task()` в `scripts/worker/task.sh`, оба воспроизведены и
закрыты здесь:

  (a) Скан по всему тексту PR вместо объявленной задачи (класс #187/#195,
      третий экземпляр): номер, упомянутый в прозе чужого PR, делал задачу
      «занятой». Кормится РЕАЛЬНЫМ телом PR #181 репозитория
      (`gh pr view 181 --json body`), который объявляет #179 первой строкой
      и упоминает #149/#90 в прозе (описывает белые пятна, не связанные с
      собой задачи) — те самые номера, что 2026-09-03 были ложно заблокированы
      старым алгоритмом task.sh (замер задачи #245).

  (b) Открытый PR без исполнителя на issue не давал задачу выбрать никогда:
      возврат в пул (`scheduler.py::unhealthy_pulls`) снимает исполнителя
      именно для того, чтобы задачу подхватили и довели существующий PR, но
      `free_task()` исключал её из пула — задача становилась «свободна
      навсегда, но невыбираема». Один критерий свободы — assignees issue.

  (c) Обратная проверка: задача с открытым PR И назначенным исполнителем
      по-прежнему недоступна — кто-то уже работает.

Мутация (доказано вручную 2026-09-03): подмена `task_ref.declared_tasks` в
`declared_pr_for_task` на скан всего текста (`references_task`/`extract_task_refs`
по телу целиком) красит test_declared_pr_ignores_prose_mention_real_pr_181 —
PR #181 начинает считаться объявляющим #149 из-за упоминания в прозе.

Запуск: python -m pytest scripts/lib/test_free_task.py -q
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).with_name("free_task.py")
spec = importlib.util.spec_from_file_location("free_task", SCRIPT)
free_task = importlib.util.module_from_spec(spec)
spec.loader.exec_module(free_task)  # type: ignore[union-attr]


# ── Прод-форма: реальное тело PR #181 (gh pr view 181 --json body), ────────────────
# объявляет #179 первой строкой, упоминает #149 и #90 в прозе описания.
PR_181_BODY = (
    "#179\n"
    "\n"
    "## Проблема\n"
    "\n"
    "`.github/ISSUE_TEMPLATE/white-spot.yml:3` ставил `labels: [white-spot]`, "
    "а пул задач\n"
    "воркера — это issues строго с меткой `task` (`scripts/orchestra/scheduler.py`,\n"
    "`scripts/orchestra/contract_check.py`). Белое пятно, заведённое по шаблону, "
    "метки\n"
    "`task` не получало и физически не попадало в пул — из 7 открытых `white-spot` "
    "четыре\n"
    "висели без `task` (#149, #90, #72, #43), в том числе #90 — сломанный гейт "
    "ревью\n"
    "(`check_pr.py: NameError LARGE_OK`), который никто не подхватывал в работу "
    "неделями.\n"
    "\n"
    "## Что сделано\n"
    "\n"
    "1. `.github/ISSUE_TEMPLATE/white-spot.yml` заводит новые issues сразу с "
    "метками\n"
    "   `[white-spot, task]`.\n"
)
PR_181 = {"number": 181, "headRefName": "agent/179-white-spot-in-pool", "body": PR_181_BODY}


def issue(number, title="задача", assignees=None):
    return {"number": number, "title": title, "assignees": [{"login": a} for a in (assignees or [])]}


# ── free_candidates / oldest_free: единственный критерий — assignees ───────────────


def test_free_candidates_excludes_assigned_and_sorts_by_number():
    issues = [issue(233, assignees=[]), issue(90, assignees=["someone"]), issue(43, assignees=[])]
    result = [i["number"] for i in free_task.free_candidates(issues)]
    assert result == [43, 233]  # 90 исключена (есть исполнитель), сортировка по номеру


def test_oldest_free_picks_lowest_number_not_newest():
    # issues API отдаёт по убыванию новизны — без сортировки воркер брал бы
    # свежайшую задачу (косметику), а не старейшую в пуле.
    issues = [issue(240, assignees=[]), issue(43, assignees=[]), issue(158, assignees=[])]
    assert free_task.oldest_free(issues)["number"] == 43


def test_oldest_free_empty_pool_is_none():
    assert free_task.oldest_free([]) is None
    assert free_task.oldest_free([issue(5, assignees=["x"])]) is None


# ── (a) прод-форма: упоминание в прозе не делает задачу «объявленной» ──────────────


def test_declared_pr_ignores_prose_mention_real_pr_181():
    # #149 и #90 упомянуты в теле PR #181 только в прозе описания белых пятен —
    # PR #181 объявляет #179, не их. Именно эти номера (#149, #90 — вместе с
    # #43, #72, #119, #120, #124, #153, #158) были ложно заблокированы старым
    # `scan("#[0-9]+")` в task.sh (замер #245).
    assert free_task.declared_pr_for_task([PR_181], 149) is None
    assert free_task.declared_pr_for_task([PR_181], 90) is None
    assert free_task.declared_pr_for_task([PR_181], 43) is None


def test_task_only_mentioned_in_prose_is_a_free_candidate():
    # Полный конвейер (a): задача #149 без исполнителя, PR #181 открыт и
    # упоминает её в прозе, но объявляет #179. Воркер обязан увидеть #149
    # свободной и НЕ привязывать её к чужому PR #181.
    issues = [issue(179, assignees=["someone"]), issue(149, assignees=[])]
    chosen = free_task.oldest_free(issues)
    assert chosen["number"] == 149
    assert free_task.declared_pr_for_task([PR_181], chosen["number"]) is None


# ── (b) задача с открытым PR и БЕЗ исполнителя выбирается и ведёт к доводке ────────


def test_task_with_open_pr_and_no_assignee_is_selected_for_continuation():
    issues = [issue(179, assignees=[])]  # исполнитель снят (unhealthy_pulls)
    chosen = free_task.oldest_free(issues)
    assert chosen is not None and chosen["number"] == 179
    pr = free_task.declared_pr_for_task([PR_181], chosen["number"])
    assert pr is not None
    assert pr["number"] == 181
    assert pr["headRefName"] == "agent/179-white-spot-in-pool"  # довести именно эту ветку


# ── реворк-переориентация (#394): ветка называет уже закрытую задачу ────────────────


PR_388_BODY = (
    "#391\n\n"
    "Related: #256 (закрыта акцептансом 2026-09-05 как «без наблюдаемого "
    "результата»; правило 2026-09-06: закрытая задача не переоткрывается, "
    "новая узкая #391 по фактическому содержимому).\n"
)
PR_388 = {"number": 388, "headRefName": "agent/256-task-rework-loop", "body": PR_388_BODY}


def test_declared_pr_for_task_finds_rework_successor_by_body_not_only_branch():
    # Живой класс #394 (PR #388/#384/#359/#167 репозитория на 2026-09-06):
    # старая задача #256 закрыта раньше срока, докрытие оформлено новой узкой
    # задачей #391, объявленной первой строкой тела — ветка переименовать
    # нельзя. Воркер обязан найти этот PR по номеру НОВОЙ задачи, иначе
    # открыл бы второй PR на неё же.
    pr = free_task.declared_pr_for_task([PR_388], 391)
    assert pr is not None and pr["number"] == 388
    # Старая (закрытая) задача из ветки тоже находит этот PR — безвредно:
    # closed-задача уже не входит в свободный пул, declared-pr для неё
    # никогда не будет запрошен воркером на практике.
    assert free_task.declared_pr_for_task([PR_388], 256)["number"] == 388


# ── (c) обратная проверка: задача с открытым PR И исполнителем не выбирается ───────


def test_task_with_open_pr_and_assignee_is_not_selected():
    issues = [issue(179, assignees=["mytab0r"])]  # кто-то уже работает
    assert free_task.oldest_free(issues) is None


# ── CLI: контракт для task.sh (tsv на stdout, коды 0/1/2) ──────────────────────────


def run_cli(args, cwd=None):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, cwd=cwd,
    )


def test_cli_oldest_free_contract(tmp_path):
    issues_file = tmp_path / "issues.json"
    issues_file.write_text(json.dumps([issue(43, title="Заголовок 43", assignees=[])]), encoding="utf-8")
    result = run_cli(["oldest-free", str(issues_file)])
    assert result.returncode == 0
    assert result.stdout.strip() == "43\tЗаголовок 43"

    empty_file = tmp_path / "empty.json"
    empty_file.write_text("[]", encoding="utf-8")
    result_empty = run_cli(["oldest-free", str(empty_file)])
    assert result_empty.returncode == 1
    assert result_empty.stdout == ""


def test_cli_declared_pr_contract(tmp_path):
    prs_file = tmp_path / "prs.json"
    prs_file.write_text(json.dumps([PR_181]), encoding="utf-8")

    found = run_cli(["declared-pr", "179", str(prs_file)])
    assert found.returncode == 0
    assert found.stdout.strip() == "181\tagent/179-white-spot-in-pool"

    not_found = run_cli(["declared-pr", "149", str(prs_file)])
    assert not_found.returncode == 1
    assert not_found.stdout == ""


def test_cli_unknown_arguments_are_rejected():
    result = run_cli(["bogus"])
    assert result.returncode == 2


# ── «пусто» vs «сломано»: rc 1 (пул пуст) и rc 2 (инструмент сломался) ─────────────
# не смешиваются (находка AI-ревью PR #247, 2026-09-03): битый JSON пула раньше
# ронял python необработанным исключением с rc=1 — той же, что у пустого пула,
# и task.sh трактовал крах как «свободных задач нет» (declared-pr — как «PR нет»,
# что вело ко второму PR на ту же задачу).


def test_cli_oldest_free_broken_pool_file_is_rc2_not_rc1(tmp_path):
    broken_file = tmp_path / "broken.json"
    broken_file.write_text("not json", encoding="utf-8")
    result = run_cli(["oldest-free", str(broken_file)])
    assert result.returncode == 2  # НЕ 1 — «сломано», не «пусто»
    assert result.stdout == ""
    assert "free_task.py" in result.stderr


def test_cli_declared_pr_broken_prs_file_is_rc2_not_rc1(tmp_path):
    broken_file = tmp_path / "broken.json"
    broken_file.write_text("not json", encoding="utf-8")
    result = run_cli(["declared-pr", "179", str(broken_file)])
    assert result.returncode == 2  # НЕ 1 — иначе воркер решит «PR нет» и откроет второй
    assert result.stdout == ""
    assert "free_task.py" in result.stderr


def test_cli_oldest_free_missing_file_is_rc2(tmp_path):
    missing_file = tmp_path / "does-not-exist.json"
    result = run_cli(["oldest-free", str(missing_file)])
    assert result.returncode == 2
    assert result.stdout == ""
