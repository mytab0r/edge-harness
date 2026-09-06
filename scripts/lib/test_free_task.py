#!/usr/bin/env python3
"""Тесты выбора свободной задачи (scripts/lib/free_task.py, #245).

Два дефекта `free_task()` в `scripts/worker/task.sh`, оба воспроизведены и
закрыты здесь:

  (a) Скан по всему тексту PR вместо задачи ветки (класс #187/#195, третий
      экземпляр): номер, упомянутый в прозе чужого PR, делал задачу
      «занятой». Кормится РЕАЛЬНЫМ телом PR #181 репозитория
      (`gh pr view 181 --json body`), ветка которого называет #179 и который
      упоминает #149/#90 в прозе (описывает белые пятна, не связанные с
      собой задачи) — те самые номера, что 2026-09-03 были ложно заблокированы
      старым алгоритмом task.sh (замер задачи #245). Решение владельца
      2026-09-06 (#394): единственный источник задачи PR — имя ветки, тело
      не читается вовсе (не только «не любое упоминание», а «никак»).

  (b) Открытый PR без исполнителя на issue не давал задачу выбрать никогда:
      возврат в пул (`scheduler.py::unhealthy_pulls`) снимает исполнителя
      именно для того, чтобы задачу подхватили и довели существующий PR, но
      `free_task()` исключал её из пула — задача становилась «свободна
      навсегда, но невыбираема». Один критерий свободы — assignees issue.

  (c) Обратная проверка: задача с открытым PR И назначенным исполнителем
      по-прежнему недоступна — кто-то уже работает.

Мутация (доказано вручную 2026-09-03, обновлено 2026-09-06): подмена
`task_ref.task_from_branch` в `declared_pr_for_task` на скан тела
(`references_task`/`extract_task_refs`) красит
test_declared_pr_ignores_prose_mention_real_pr_181 — PR #181 начинает
считаться объявляющим #149 из-за упоминания в прозе.

Запуск: python -m pytest scripts/lib/test_free_task.py -q
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

SCRIPT = Path(__file__).with_name("free_task.py")
spec = importlib.util.spec_from_file_location("free_task", SCRIPT)
free_task = importlib.util.module_from_spec(spec)
spec.loader.exec_module(free_task)  # type: ignore[union-attr]

TASK_DEPS_SCRIPT = Path(__file__).with_name("task_deps.py")
_td_spec = importlib.util.spec_from_file_location("task_deps", TASK_DEPS_SCRIPT)
task_deps = importlib.util.module_from_spec(_td_spec)
_td_spec.loader.exec_module(task_deps)  # type: ignore[union-attr]


# ── Прод-форма: реальное тело PR #181 (gh pr view 181 --json body), ────────────────
# ветка называет #179; тело упоминает #149 и #90 в прозе описания (тело не
# читается резолвером вовсе, приведено только чтобы прод-форма была честной).
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


def issue(number, title="задача", assignees=None, labels=None, blocking_open=0):
    # labels отсутствует в результате, когда не передан явно (не пустой
    # список) — воспроизводит REST-форму без ключа labels вовсе, на которую
    # опирается test_free_candidates_keeps_issue_without_labels_key.
    result = {
        "number": number,
        "title": title,
        "assignees": [{"login": a} for a in (assignees or [])],
        "blocking_open": blocking_open,
    }
    if labels is not None:
        result["labels"] = [{"name": name} for name in labels]
    return result


# ── free_candidates / oldest_free: assignees, замок, waiting:owner ─────────────────


def test_free_candidates_excludes_assigned_is_a_filter_not_a_sort():
    # free_candidates — только фильтр (#361): сортировку по приоритету делает
    # prioritized_free/oldest_free, не эта функция (иначе порядок задавался
    # бы в двух местах).
    issues = [issue(233, assignees=[]), issue(90, assignees=["someone"]), issue(43, assignees=[])]
    result = {i["number"] for i in free_task.free_candidates(issues)}
    assert result == {43, 233}  # 90 исключена (есть исполнитель)


def test_oldest_free_picks_lowest_number_not_newest():
    # issues API отдаёт по убыванию новизны — без сортировки воркер брал бы
    # свежайшую задачу (косметику), а не старейшую в пуле. Без меты/графа
    # (#361) уровень 3 (номер) — единственный отличающий уровень.
    issues = [issue(240, assignees=[]), issue(43, assignees=[]), issue(158, assignees=[])]
    assert free_task.oldest_free(issues)["number"] == 43


# ── Приоритет #361: три уровня, в этом порядке ──────────────────────────────


def test_level1_meta_label_wins_regardless_of_blocking_count():
    meta = issue(100, labels=["area:process"], blocking_open=0)
    applied = issue(50, blocking_open=10)  # блокирует больше, но не мета
    result = free_task.prioritized_free([applied, meta])
    assert [i["number"] for i in result] == [100, 50]


def test_level2_blocking_count_orders_within_same_meta_level():
    blocks_many = issue(200, blocking_open=5)
    blocks_none = issue(150, blocking_open=0)
    result = free_task.prioritized_free([blocks_none, blocks_many])
    assert [i["number"] for i in result] == [200, 150]


def test_level3_number_is_tiebreak_when_meta_and_blocking_tie():
    older = issue(50, blocking_open=2)
    newer = issue(90, blocking_open=2)
    result = free_task.prioritized_free([newer, older])
    assert [i["number"] for i in result] == [50, 90]


def test_mixed_meta_with_one_blocked_beats_applied_with_ten():
    # Ровно кейс, названный владельцем: мета с одним блокируемым обгоняет
    # прикладную с десятью — мета-уровень решает раньше числа блокируемых.
    meta_light = issue(300, labels=["area:process"], blocking_open=1)
    applied_heavy = issue(10, blocking_open=10)
    result = free_task.prioritized_free([applied_heavy, meta_light])
    assert [i["number"] for i in result] == [300, 10]


def test_graph_is_empty_true_when_nobody_blocks_and_nobody_is_meta():
    issues = [issue(1), issue(2), issue(3)]
    assert free_task.graph_is_empty(issues) is True


def test_graph_is_empty_false_when_one_candidate_blocks_something():
    issues = [issue(1, blocking_open=1), issue(2)]
    assert free_task.graph_is_empty(issues) is False


def test_graph_is_empty_false_when_one_candidate_is_meta():
    issues = [issue(1, labels=["area:process"]), issue(2)]
    assert free_task.graph_is_empty(issues) is False


def test_cli_warns_on_stderr_when_graph_empty(tmp_path):
    issues_file = tmp_path / "issues.json"
    issues_file.write_text(json.dumps([issue(43, title="т")]), encoding="utf-8")
    result = run_cli(["oldest-free", str(issues_file)])
    assert result.returncode == 0
    assert result.stdout.strip() == "43\tт"
    assert "граф блокировок пуст" in result.stderr


def test_cli_silent_when_graph_has_signal(tmp_path):
    issues_file = tmp_path / "issues.json"
    issues_file.write_text(
        json.dumps([issue(43, title="т", blocking_open=1)]), encoding="utf-8",
    )
    result = run_cli(["oldest-free", str(issues_file)])
    assert result.returncode == 0
    assert "граф блокировок пуст" not in result.stderr


def _pool_page(blocker_edges):
    """Форма ответа GraphQL `task_deps._POOL_QUERY_TMPL` с двумя задачами:
    #10 (блокирует узлы `blocker_edges`) и контрольная #20 (блокирует 2
    открытых, не меняется по ходу теста)."""
    return {
        "repository": {
            "issues": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [
                    {
                        "number": 10, "title": "блокирующая",
                        "labels": {"nodes": []}, "assignees": {"nodes": []},
                        "blockedBy": {"nodes": []},
                        "blocking": {"nodes": [
                            {"number": n, "state": s} for n, s in blocker_edges
                        ]},
                    },
                    {
                        "number": 20, "title": "контрольная",
                        "labels": {"nodes": []}, "assignees": {"nodes": []},
                        "blockedBy": {"nodes": []},
                        "blocking": {"nodes": [
                            {"number": 200, "state": "OPEN"},
                            {"number": 201, "state": "OPEN"},
                        ]},
                    },
                ],
            }
        }
    }


def test_mutation_closing_blocked_issues_flips_priority_order_next_run():
    # Авто-возврат (#361, п.5): "blocking_open" — живой пересчёт (сколько
    # ОТКРЫТЫХ задач блокирует эта СЕЙЧАС), не кэш и не метка, которую надо
    # снимать руками. Прогнано через прод-цепочку целиком: task_deps.fetch_pool
    # (GraphQL, monkeypatch subprocess — тот же приём, что test_task_deps.py) ->
    # free_task.prioritized_free, а не пересчёт локальной копией формулы.
    edges_before = [(90, "OPEN"), (91, "OPEN"), (92, "OPEN")]
    edges_after = [(90, "CLOSED"), (91, "CLOSED"), (92, "OPEN")]  # 2 из 3 закрылись

    def gh_call_before(*args):
        assert args[0] == "graphql"
        return {"data": _pool_page(edges_before)}

    def gh_call_after(*args):
        assert args[0] == "graphql"
        return {"data": _pool_page(edges_after)}

    # ДО закрытия: #10 блокирует больше открытых (3 > 2) — выбирается первым.
    pool_before = task_deps.fetch_pool("owner/repo", gh_call=gh_call_before)
    assert [i["number"] for i in pool_before] == [10, 20]
    assert pool_before[0]["blocking_open"] == 3
    assert free_task.prioritized_free(pool_before)[0]["number"] == 10

    # Мутация: две из трёх блокируемых задачами #10 закрылись — следующий
    # прогон `fetch_pool` (не ручная правка поля) видит счётчик 1 < 2.
    pool_after = task_deps.fetch_pool("owner/repo", gh_call=gh_call_after)
    assert pool_after[0]["blocking_open"] == 1

    # ПОСЛЕ: #10 блокирует меньше, чем #20 (1 < 2) — порядок ПЕРЕВОРАЧИВАЕТСЯ,
    # без правки кода/метки/ручного вмешательства — только следующий пересчёт.
    assert free_task.prioritized_free(pool_after)[0]["number"] == 20


def test_oldest_free_empty_pool_is_none():
    assert free_task.oldest_free([]) is None
    assert free_task.oldest_free([issue(5, assignees=["x"])]) is None


def test_free_candidates_excludes_waiting_owner_without_assignee():
    """Находка AI-ревью PR #471 (#470): задача без исполнителя, но с меткой
    waiting:owner, иначе прошла бы фильтр «нет assignees» как свободная —
    oldest_free выбирал бы ровно её каждый пульс, claim() отказывал бы, и
    задачи ЗА ней в очереди не брались бы вовсе (класс #255). Мутация:
    убери условие `_has_waiting_owner_label` в free_candidates — этот тест
    краснеет (233 возвращается в списке кандидатов)."""
    issues = [
        issue(43, assignees=[]),
        issue(233, assignees=[], labels=["task", "waiting:owner"]),
    ]
    result = [i["number"] for i in free_task.free_candidates(issues)]
    assert result == [43]


def test_free_candidates_keeps_issue_without_labels_key():
    """Issue без ключа `labels` вовсе (старые фикстуры/вызовы) не считается
    несущей waiting:owner — то же допущение, что уже применяет `assignees`."""
    issues = [issue(43, assignees=[])]
    assert [i["number"] for i in free_task.free_candidates(issues)] == [43]


def test_oldest_free_skips_waiting_owner_to_next_candidate():
    """Симметрично test_free_candidates_excludes_waiting_owner_without_assignee,
    но через oldest_free (то, что реально вызывает task.sh): старейшая задача
    несёт waiting:owner — воркер обязан получить СЛЕДУЮЩУЮ, не None и не её."""
    issues = [
        issue(43, assignees=[], labels=["task", "waiting:owner"]),
        issue(233, assignees=[]),
    ]
    assert free_task.oldest_free(issues)["number"] == 233


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


# ── ветка называет уже закрытую задачу — тело не подхватывается (#394) ──────────────


PR_388_BODY = (
    "#391\n\n"
    "Related: #256 (закрыта акцептансом 2026-09-05 как «без наблюдаемого "
    "результата»; правило 2026-09-06: закрытая задача не переоткрывается, "
    "новая узкая #391 по фактическому содержимому).\n"
)
PR_388 = {"number": 388, "headRefName": "agent/256-task-rework-loop", "body": PR_388_BODY}


def test_declared_pr_for_task_does_not_find_rework_successor_by_body():
    # Живой случай (PR #388/#384/#359/#167 репозитория на 2026-09-06): старая
    # задача #256 закрыта раньше срока, докрытие оформлено новой узкой
    # задачей #391, первой строкой тела — ветку переименовать нельзя.
    # Решение владельца 2026-09-06: тело не читается вовсе, поэтому PR не
    # находится по номеру НОВОЙ задачи — для #391 нужна НОВАЯ ветка
    # (scripts/git/task-branch 391-slug), не тот же PR.
    assert free_task.declared_pr_for_task([PR_388], 391) is None
    # Задача из ветки по-прежнему находится, даже закрытая — declared_pr_for_task
    # не проверяет состояние issue, только ветку.
    assert free_task.declared_pr_for_task([PR_388], 256)["number"] == 388


# ── (c) обратная проверка: задача с открытым PR И исполнителем не выбирается ───────


# ── (d) задача с конфликтным PR исключена из общего выбора (находка ревью PR #478) ──
# scheduler.py::dispatch_conflict_rework снимает assignee+замок ИМЕННО чтобы
# довести PR адресно (вход `task=N`, бюджет РОВНО одна попытка). Без этого
# фильтра generic-пульс (free_task() без --task) мог бы взять ту же задачу
# мимо бюджета, если адресный прогон освободил её (упал по квоте/крашу) до
# того, как снова занял.


def pr(number, ref, labels=()):
    return {"number": number, "headRefName": ref, "body": "", "labels": [{"name": n} for n in labels]}


def test_conflict_declared_tasks_finds_task_by_branch_and_label():
    prs = [
        pr(560, "agent/474-conflict-auto-rebase", labels=["conflict", "review:ok"]),
        pr(561, "agent/475-something", labels=["review:ok"]),  # не конфликтует
        pr(562, "dependabot/npm/foo", labels=["conflict"]),  # не agent-ветка — не задача
    ]
    assert free_task.conflict_declared_tasks(prs) == {474}


def test_free_candidates_excludes_conflict_declared_task_even_when_oldest(monkeypatch):
    # Мутация: убери фильтр `excluded` в free_candidates — этот тест покраснеет
    # (задача #43 снова оказалась бы выбрана как старейшая свободная).
    issues = [issue(43, assignees=[]), issue(90, assignees=[])]
    excluded = free_task.conflict_declared_tasks(
        [pr(560, "agent/43-conflicted", labels=["conflict"])])
    assert excluded == {43}
    chosen = free_task.oldest_free(issues, excluded=excluded)
    assert chosen is not None and chosen["number"] == 90  # не #43 — она конфликтует


def test_free_candidates_excluded_defaults_to_empty_set_backward_compatible():
    # Вызывающий код, ещё не переданный на новый параметр (три прежних
    # позиционных теста файла), обязан продолжать работать без изменений.
    issues = [issue(43, assignees=[]), issue(90, assignees=[])]
    assert [i["number"] for i in free_task.free_candidates(issues)] == [43, 90]


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


def test_cli_conflict_tasks_contract(tmp_path):
    prs_file = tmp_path / "prs.json"
    prs_file.write_text(json.dumps([
        {"number": 560, "headRefName": "agent/474-x", "body": "",
         "labels": [{"name": "conflict"}]},
        {"number": 561, "headRefName": "agent/475-y", "body": "", "labels": []},
    ]), encoding="utf-8")
    result = run_cli(["conflict-tasks", str(prs_file)])
    assert result.returncode == 0
    assert result.stdout.strip() == "474"

    empty_file = tmp_path / "empty.json"
    empty_file.write_text("[]", encoding="utf-8")
    empty_result = run_cli(["conflict-tasks", str(empty_file)])
    assert empty_result.returncode == 0
    assert empty_result.stdout.strip() == ""


def test_cli_oldest_free_excludes_conflict_declared_task(tmp_path):
    # Контракт task.sh целиком: oldest-free с третьим позиционным аргументом
    # (excluded) — тот же формат, что locked (номера через пробел).
    issues_file = tmp_path / "issues.json"
    issues_file.write_text(json.dumps([
        issue(43, title="конфликтная", assignees=[]),
        issue(90, title="обычная", assignees=[]),
    ]), encoding="utf-8")
    result = run_cli(["oldest-free", str(issues_file), "", "43"])
    assert result.returncode == 0
    assert result.stdout.strip() == "90\tобычная"


def test_cli_unknown_arguments_are_rejected():
    result = run_cli(["bogus"])
    assert result.returncode == 2


def test_task_sh_wires_conflict_exclusion_into_free_task(monkeypatch):
    # Гвардия по исходнику (класс тот же, что test_task_sh_composes_claim_via_
    # worker_run_format в test_scheduler.py): не бас-тест поведения bash (для
    # этого понадобился бы полноценный интеграционный прогон), а доказательство,
    # что task.sh реально ЗОВЁТ conflict-tasks и передаёт результат в
    # oldest-free — легко забыть при рефакторинге, раз exclusion живёт в
    # отдельном вызове, а не внутри free_task.py::oldest_free по умолчанию.
    task_sh = (Path(__file__).with_name("..") / "worker" / "task.sh").resolve().read_text(encoding="utf-8")
    assert 'free_task.py" conflict-tasks' in task_sh
    assert 'oldest-free "$issues_file" "$locked" "$excluded"' in task_sh
    # labels обязателен в запросе PR — без него conflict-tasks увидит labels=[]
    # и фильтр молча не сработает никогда (silent-wrong, не сбой).
    assert "gh pr list --state open --limit 100 --json number,body,headRefName,labels" in task_sh


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
