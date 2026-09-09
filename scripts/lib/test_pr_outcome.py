#!/usr/bin/env python3
"""Тесты признака успеха прогона воркера по ветке задачи (scripts/lib/pr_outcome.py, #413).

Живой случай: `worker/task.sh` считал успехом только ОТКРЫТЫЙ PR по ветке
задачи (`gh pr list --state open`). Прогон 34002439672 (задача #170, PR #402)
открыл PR и сам же его слил за один вызов DSH — проверка не нашла PR именно
потому, что работа доведена до конца лучше ожидаемого, и воркер напечатал
ложный провал «dsh завершился с кодом 0 без открытого PR».

Мутация, доказывающая класс (см. test_mutation_state_all_vs_open_only ниже):
если бы `classify()` считал успехом только `state == "OPEN"` (старое
поведение), тест на слитый PR (#402-форма) красится.

Запуск: python -m pytest scripts/lib/test_pr_outcome.py -q
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).with_name("pr_outcome.py")
spec = importlib.util.spec_from_file_location("pr_outcome", SCRIPT)
pr_outcome = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pr_outcome)  # type: ignore[union-attr]


def pr(number, state, additions=10, deletions=1, changed_files=1, url=None):
    return {
        "number": number,
        "state": state,
        "additions": additions,
        "deletions": deletions,
        "changedFiles": changed_files,
        "url": url or f"https://github.com/mytab0r/edge-harness/pull/{number}",
    }


# ── Живой случай: PR #402 (задача #170) открыт и слит в одном прогоне ─────────────


def test_merged_pr_is_success_not_failure():
    # Прод-форма прогона 34002439672: PR #402 состояние MERGED (mergedAt
    # 2026-09-06T01:29:22Z). Признак успеха обязан считать это успехом.
    pr_402 = pr(402, "MERGED")
    status, chosen = pr_outcome.pick_pr_outcome([pr_402])
    assert status == "merged"
    assert chosen["number"] == 402


def test_open_pr_with_diff_is_still_success():
    status, chosen = pr_outcome.pick_pr_outcome([pr(9, "OPEN")])
    assert status == "open"
    assert chosen["number"] == 9


# ── Настоящий провал не должен потеряться ──────────────────────────────────────────


def test_no_pr_at_all_is_absent_failure():
    status, chosen = pr_outcome.pick_pr_outcome([])
    assert status == "absent"
    assert chosen is None


def test_closed_unmerged_pr_is_failure():
    # Закрыт без слияния — работу выбросили, ветка не несёт результата.
    status, _ = pr_outcome.pick_pr_outcome([pr(11, "CLOSED")])
    assert status == "absent"


def test_open_pr_without_diff_is_failure_not_success():
    # Промежуточный случай владельца: PR открыт, но пуст (DSH создал ветку/PR,
    # не поработав над задачей) — это провал, не подмена «PR существует».
    # Пустая ветка = ни текстового диффа, ни файлов вовсе.
    status, chosen = pr_outcome.pick_pr_outcome([pr(12, "OPEN", additions=0, deletions=0, changed_files=0)])
    assert status == "empty"
    assert chosen["number"] == 12


def test_open_pr_binary_only_change_is_success_not_empty():
    # Находка ревью PR #415: additions=deletions=0 бывает и у ЖИВОЙ работы —
    # PR меняет только бинарник или делает чистое переименование. changedFiles
    # > 0 при нулевом текстовом диффе обязан считаться успехом, не «пусто».
    status, chosen = pr_outcome.pick_pr_outcome(
        [pr(13, "OPEN", additions=0, deletions=0, changed_files=1)])
    assert status == "open"
    assert chosen["number"] == 13


# ── Выбор среди нескольких PR на одну ветку: MERGED побеждает всегда ──────────────


def test_merged_wins_over_open_regardless_of_order():
    prs = [pr(20, "OPEN"), pr(21, "MERGED")]
    status, chosen = pr_outcome.pick_pr_outcome(prs)
    assert status == "merged" and chosen["number"] == 21

    status2, chosen2 = pr_outcome.pick_pr_outcome(list(reversed(prs)))
    assert status2 == "merged" and chosen2["number"] == 21


def test_open_wins_over_empty_and_absent():
    prs = [
        pr(30, "CLOSED"),
        pr(31, "OPEN", additions=0, deletions=0, changed_files=0),
        pr(32, "OPEN"),
    ]
    status, chosen = pr_outcome.pick_pr_outcome(prs)
    assert status == "open" and chosen["number"] == 32


# ── Мутация: «только open» вместо «open ИЛИ merged» красит живой случай ───────────


def test_mutation_state_all_vs_open_only():
    """Доказательство, что фикс закрывает именно этот класс: старая проверка
    (`state == "OPEN"` — единственный успех) на форме PR #402 отвечает
    failure. Снятие фикса красит этот тест."""
    pr_402 = pr(402, "MERGED")

    def old_broken_classify(pull):
        return "open" if pull.get("state") == "OPEN" else "absent"

    assert old_broken_classify(pr_402) == "absent"  # старое поведение — ложный провал
    assert pr_outcome.classify(pr_402) == "merged"  # новое — верный успех


# ── CLI: контракт для task.sh (tsv на stdout, коды 0 успех / 1 провал / 2 сломано) ─


def run_cli(args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, encoding="utf-8",
    )


def test_cli_merged_is_exit_0(tmp_path):
    prs_file = tmp_path / "prs.json"
    prs_file.write_text(json.dumps([pr(402, "MERGED")]), encoding="utf-8")
    result = run_cli([str(prs_file)])
    assert result.returncode == 0
    assert result.stdout.strip() == "merged\thttps://github.com/mytab0r/edge-harness/pull/402"


def test_cli_no_pr_at_all_is_exit_1(tmp_path):
    prs_file = tmp_path / "prs.json"
    prs_file.write_text("[]", encoding="utf-8")
    result = run_cli([str(prs_file)])
    assert result.returncode == 1
    assert result.stdout == "absent\t\n"


def test_cli_broken_json_is_exit_2_not_1(tmp_path):
    # «Пусто» (нет PR) и «сломано» (битый JSON/сеть) — разные состояния
    # (см. free_task.py, тот же принцип): не смешивать в один код возврата.
    broken_file = tmp_path / "broken.json"
    broken_file.write_text("not json", encoding="utf-8")
    result = run_cli([str(broken_file)])
    assert result.returncode == 2
    assert result.stdout == ""
    assert "pr_outcome.py" in result.stderr


def test_cli_missing_args_is_exit_2():
    result = run_cli([])
    assert result.returncode == 2
