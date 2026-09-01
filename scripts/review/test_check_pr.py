#!/usr/bin/env python3
"""Гвардия аргумента --tree в детерминированном ревью (bootstrap PR #138).

Класс, который ловит этот тест: pr-review исполняет check_pr.py из
доверенного чекаута main (см. .github/workflows/pr-review.yml), а не из
дерева проверяемого PR. Если main откатится к сигнатуре без --tree, PR #138
(который зовёт `check_pr.py --pr N --tree pr-tree`) снова упрётся в
bootstrap-тупик "unrecognized arguments: --tree" — тот самый инцидент, ради
которого этот аргумент внесён отдельным PR.

Кормится прод-формой вызова: реальный subprocess того же скрипта, теми же
аргументами, что кладёт .github/workflows/pr-review.yml — а не пересказом
через самодельный parser (такой тест не ловит регресс в самом check_pr.py).
Сеть не нужна: GITHUB_REPOSITORY отсутствует в окружении теста, поэтому
скрипт падает уже ПОСЛЕ разбора аргументов (KeyError в main(), rc=1) — это
и есть граница между «argparse принял флаг» и «код полез в сеть». Если
argparse флаг отвергает, скрипт падает раньше и иначе (rc=2, usage на
stderr) — это и отличает регресс от нормального сетевого сбоя.

Запуск: python -m pytest scripts/review/test_check_pr.py -q
"""

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).with_name("check_pr.py")


def _run(*extra_args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--pr", "138", *extra_args],
        capture_output=True, text=True,
        env={"PATH": __import__("os").environ.get("PATH", "")},  # без GITHUB_REPOSITORY
    )


def test_tree_flag_accepted_reaches_main_body():
    # Прод-вызов PR #138: `check_pr.py --pr N --tree pr-tree`. argparse обязан
    # принять флаг и пропустить выполнение внутрь main() — граница успеха
    # здесь не "review: OK", а "дошли до сетевого кода", то есть KeyError на
    # GITHUB_REPOSITORY, а не argparse-ошибка неизвестного аргумента.
    result = _run("--tree", "pr-tree")
    assert result.returncode == 1, (
        f"--tree отвергнут или сломал разбор аргументов: rc={result.returncode}\n{result.stderr}"
    )
    assert "unrecognized arguments" not in result.stderr
    assert "GITHUB_REPOSITORY" in result.stderr


def test_without_tree_flag_behaves_same_as_before():
    # Текущий прод-вызов .github/workflows/pr-review.yml: без --tree вообще.
    # Дефолт обязан оставить поведение прежним — падение в той же точке.
    result = _run()
    assert result.returncode == 1
    assert "GITHUB_REPOSITORY" in result.stderr


def test_unknown_flag_still_rejected_by_argparse():
    # Контроль: argparse в принципе различает валидные и невалидные флаги —
    # без этого теста выше ничего бы не доказывали.
    result = _run("--no-such-flag", "x")
    assert result.returncode == 2
    assert "unrecognized arguments" in result.stderr
