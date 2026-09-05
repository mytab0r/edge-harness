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


def issue(number, title="задача", assignees=None, labels=None, blocking_open=0):
    return {
        "number": number,
        "title": title,
        "assignees": [{"login": a} for a in (assignees or [])],
        "labels": [{"name": name} for name in (labels or [])],
        "blocking_open": blocking_open,
    }


# ── free_candidates / oldest_free: единственный критерий — assignees ───────────────


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


def test_mutation_closing_blocked_issues_flips_priority_order_next_run():
    # Авто-возврат (#361, п.5): "blocking_open" — живой пересчёт (сколько
    # ОТКРЫТЫХ задач блокирует эта СЕЙЧАС), не кэш и не метка, которую надо
    # снимать руками. task_deps.fetch_pool считает только state == OPEN на
    # КАЖДОМ прогоне — закрытие блокируемых задач меняет счётчик, а значит и
    # порядок, следующим же запуском без единого дополнительного действия.
    def blocking_open_from_native(nodes):
        return sum(1 for node in nodes if node["state"] == "OPEN")

    blocker_before = [{"number": 90, "state": "OPEN"}, {"number": 91, "state": "OPEN"},
                       {"number": 92, "state": "OPEN"}]
    blocker_after = [{"number": 90, "state": "CLOSED"}, {"number": 91, "state": "CLOSED"},
                      {"number": 92, "state": "OPEN"}]  # 2 из 3 блокируемых закрылись
    assert blocking_open_from_native(blocker_before) == 3
    assert blocking_open_from_native(blocker_after) == 1

    blocker = issue(10, blocking_open=blocking_open_from_native(blocker_before))
    other = issue(20, blocking_open=2)  # не меняется весь тест — контрольная величина

    # ДО закрытия: 10 блокирует больше (3 > 2) — выбирается первым.
    assert free_task.prioritized_free([other, blocker])[0]["number"] == 10

    # Мутация: блокирующая задача №10 теряет два открытых блокируемых.
    blocker["blocking_open"] = blocking_open_from_native(blocker_after)

    # ПОСЛЕ: 10 блокирует меньше, чем other (1 < 2) — порядок ПЕРЕВОРАЧИВАЕТСЯ,
    # без правки кода/метки/ручного вмешательства — только следующий пересчёт.
    assert free_task.prioritized_free([other, blocker])[0]["number"] == 20


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
