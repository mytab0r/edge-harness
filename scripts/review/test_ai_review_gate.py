#!/usr/bin/env python3
"""Гвардия гейта первого ревью в ai-review.yml (#204, второй заход).

Класс проблемы: шаг `facts` открывает дорогой прогон AI-ревью только когда
считает нужным (`go=true`), и это решение принимается bash-логикой ВНУТРИ
самого workflow — до какого-либо чекаута, до появления в рабочем дереве
scripts/lib/review_labels.py. Юнит-тест на python-функцию тут ничего не
доказывает: производственный код живёт в YAML, а не в модуле. Тест обязан
исполнить РЕАЛЬНЫЙ `run:`-скрипт шага `facts`, а не его пересказ (AGENTS.md:
«тест кормит прод-форму данных, а не пересказ»).

Живой замер бага, который это ловит: PR #387/#384/#367/#262 (#204, повторная
находка 2026-09-06) простояли с `review:large` без единого ai:*-события в
таймлайне — потому что facts-шаг открывал `go=true` ТОЛЬКО по `review:ok`,
а `review:large` эту метку по построению исключает (check_pr.py::verdict_for
ставит либо review:ok, либо review:large — никогда обе). Газ #204
(`ai_review.py::apply_large_ok`) физически не мог сработать: до него не
доходил ни один прогон AI-ревью.

gh CLI подменяется bash-функцией (`export -f gh`), а не отдельным
исполняемым файлом на PATH — так тест одинаково работает и на раннерах CI
(bash на Ubuntu), и локально на Windows под git-bash, где биты
исполняемости файла не всегда доживают до `$PATH`-резолюции.

Запуск: python -m pytest scripts/review/test_ai_review_gate.py -q
"""

import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ai-review.yml"


def bash_executable() -> str:
    """Путь к настоящему POSIX bash. На Windows "bash" на PATH может
    разрешиться в WSL-лончер (C:\\Windows\\System32\\bash.exe), который падает
    без WSL и без единого читаемого символа в выводе (UTF-16 ошибка сервиса
    Bash) — git-bash предпочитается явным путём, если он на месте. На
    CI-раннере (ubuntu-latest) такого пути нет, и вызов падает на голое
    "bash" — ровно тот же исполняемый файл, что запускает шаги workflow."""
    git_bash = Path("C:/Program Files/Git/usr/bin/bash.exe")
    return str(git_bash) if git_bash.exists() else "bash"


def facts_run_script() -> str:
    """`run:` скрипт шага `facts` job'а `review` — как он реально исполняется
    раннером, без единого символа пересказа."""
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = doc["jobs"]["review"]["steps"]
    step = next(s for s in steps if s.get("id") == "facts")
    return step["run"]


# Стаб gh CLI: bash-функция, не бинарник — переживает Windows/git-bash без
# возни с правами исполнения на временный файл. Отвечает только на два
# вызова, которые реально делает facts-шаг в ветке workflow_dispatch: голову
# PR и метки issue. Любой другой вызов — громкий отказ (fail loud), не молчание.
GH_STUB = r"""
gh() {
  if [ "$1" != "api" ]; then
    echo "неожиданный вызов gh: $*" >&2
    return 1
  fi
  case "$2" in
    *pulls/*) echo "deadbeef0123456789abcdef" ;;
    *issues/*/labels) printf '%s' "$FAKE_LABELS" ;;
    *) echo "неожиданный gh api эндпоинт: $2" >&2; return 1 ;;
  esac
}
export -f gh
"""


def run_facts(labels: str, tmp_path: Path) -> dict[str, str]:
    """Гоняет реальный run-скрипт шага facts под fake gh, возвращает разобранный
    $GITHUB_OUTPUT (пары key=value)."""
    output_file = tmp_path / "github_output"
    output_file.write_text("", encoding="utf-8")
    script = GH_STUB + "\nset -o pipefail\n" + facts_run_script()
    env = {
        "PATH": subprocess.os.environ.get("PATH", ""),
        "HOME": subprocess.os.environ.get("HOME", ""),
        "EVENT_NAME": "workflow_dispatch",
        "PR_INPUT": "42",
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_OUTPUT": str(output_file),
        "FAKE_LABELS": labels,
        # Ветка workflow_run этим тестом не покрывается — EVENT_NAME
        # фиксирован на dispatch, где номер PR приходит явным входом, а не
        # разбором ветки/владельца головы.
    }
    result = subprocess.run([bash_executable(), "-c", script], env=env,
                             capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, (
        f"facts-шаг упал (rc={result.returncode}): stdout={result.stdout!r} "
        f"stderr={result.stderr!r}")
    out = {}
    for line in output_file.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            out[key] = value
    return out


@pytest.mark.parametrize("labels,expected_go", [
    ("review:ok", "true"),
    # #204, второй заход: крупный дифф без находок несёт review:large, НЕ
    # review:ok (взаимоисключающие метки одного вердикта) — гейт первого
    # ревью обязан открываться и на нём, иначе газ #204 (apply_large_ok)
    # никогда не достигает своего единственного триггера.
    ("review:large", "true"),
    ("review:ok review:large", "true"),
    ("review:changes-requested", "false"),
    ("", "false"),
])
def test_facts_gate_opens_on_review_ok_or_review_large(tmp_path, labels, expected_go):
    out = run_facts(labels, tmp_path)
    assert out.get("go") == expected_go, (
        f"labels={labels!r} → go={out.get('go')!r}, ожидали {expected_go!r} "
        f"(полный вывод: {out})")
    if expected_go == "true":
        assert out.get("pr") == "42"
        assert out.get("head") == "deadbeef0123456789abcdef"
