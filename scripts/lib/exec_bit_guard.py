#!/usr/bin/env python3
"""Гвардия класса «скрипт без бита исполнения молча роняет шаг workflow» (#510/#516).

Образцовый случай (#510): `scripts/gh/wake_orchestra.sh` попал в main с режимом
100644 — без бита исполнения. Шаг «Разбудить оркестратор» в `pr-review.yml`/
`ai-review.yml` вызывает файл НАПРЯМУЮ (`run: scripts/gh/wake_orchestra.sh ...`,
без `bash`/`python`/`node` перед путём) — такой шаг падает `Permission denied`
на КАЖДОМ прогоне. Падение невидимо: шаг часто `continue-on-error`, красным не
светится, в отчёты не попадает — нашёл владелец, спросив «как успехи», после
двух часов простоя событийного будильника.

Признак прямого вызова: путь под `scripts/` — ПЕРВОЕ слово shell-statement
(после разделителей `&& || ; |` и переноса строки, `$(`, `` ` ``, `(`, пропуская
ведущие присваивания `VAR=value`). Если тот же путь вызван ЧЕРЕЗ интерпретатор
(`bash scripts/foo.sh`), первое слово statement — сам интерпретатор, а путь —
второй токен, который эта гвардия НЕ проверяет: там бит исполнения не нужен.

Разбор `run:`-текста (`direct_script_invocations`) — чистая функция без IO,
доказана мутацией на реальных фрагментах `ai-review.yml`/`pr-review.yml`/
`plugin-forge.yml` (verbatim, не пересказ). Проверка бита (`check_exec_bit`)
тоже чистая — принимает уже загруженную карту режимов `path -> git-mode`,
IO-обвязка одна: `git ls-tree -r HEAD -- scripts` (один вызов на весь прогон,
локальный git, БЕЗ обращения к GitHub API — цена 0 запросов к квоте).

Запуск:
  python scripts/lib/exec_bit_guard.py             # печать отчёта, exit 1 при нарушении
  python -m pytest scripts/lib/test_exec_bit_guard.py -q
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import re
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

# Директории, чьи файлы реально вызываются напрямую (без интерпретатора) из
# workflow — узкий охват по природе класса (см. докстринг): скрипты
# scripts/gh, scripts/git и т.п. Расширять по факту нового прямого вызова
# вне этого списка, а не заранее — список исчерпывающе читается тестом
# test_known_direct_invocations_are_covered.
SCANNED_PREFIXES = ("scripts/",)

_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*$")
_STATEMENT_SPLIT_RE = re.compile(r"&&|\|\||[;|\n`]|\$\(|(?<!\\)\(")
_HEREDOC_START_RE = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?")
# Токен-кандидат: относительный путь repo (без ведущих $/переменных, без
# кавычек), минимум один '/'.
_PATH_TOKEN_RE = re.compile(r"^\.?/?[\w.-]+(?:/[\w.-]+)+$")


def _strip_heredocs(text: str) -> str:
    """Тело heredoc (`<<'MARKER' ... MARKER`) — данные ДРУГОГО интерпретатора
    (встроенный python/etc. — см. repo-ci.yml «Все workflows — валидный YAML»),
    не shell-statement'ы. Без вырезания первая строка тела читалась бы как
    новый statement после разделителя «перенос строки» и подмешивала бы
    случайные слова чужого языка в разбор."""
    out_lines = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        out_lines.append(line)
        match = _HEREDOC_START_RE.search(line)
        if match:
            marker = match.group(1)
            i += 1
            while i < len(lines) and lines[i].strip() != marker:
                i += 1
            # сама строка-маркер тоже не несёт statement'ов — пропускаем её
        i += 1
    return "\n".join(out_lines)


def _strip_comments(text: str) -> str:
    """`# ...` до конца строки — не команда. Эвристика (не учитывает '#' внутри
    кавычек) осознанно простая: корпус run-блоков этого репозитория не кладёт
    '#' в значимые для гвардии позиции внутри кавычек рядом с путями scripts/."""
    return "\n".join(re.sub(r"(^|\s)#.*$", "", line) for line in text.split("\n"))


def command_tokens(run_text: str) -> list[str]:
    """Первое слово каждого shell-statement run-блока, пропуская ведущие
    присваивания `VAR=value` (их может быть несколько подряд, как в
    deploy-dsh-edge.yml: `PLUGIN_ID=$id STATE=building bash ...`)."""
    text = _strip_comments(_strip_heredocs(run_text))
    tokens = []
    for part in _STATEMENT_SPLIT_RE.split(text):
        words = part.split()
        idx = 0
        while idx < len(words) and _ASSIGNMENT_RE.match(words[idx]):
            idx += 1
        if idx < len(words):
            word = words[idx].strip("()\"'`")
            if word:
                tokens.append(word)
    return tokens


def direct_script_invocations(run_text: str) -> list[str]:
    """Пути под SCANNED_PREFIXES, вызванные НАПРЯМУЮ (первым словом statement),
    без интерпретатора перед собой."""
    candidates = []
    for token in command_tokens(run_text):
        if not _PATH_TOKEN_RE.match(token):
            continue
        if not token.startswith(SCANNED_PREFIXES):
            continue
        candidates.append(token)
    return candidates


def iter_workflow_run_steps(workflows_dir: Path = WORKFLOWS_DIR) -> list[tuple[str, str, str]]:
    """IO: (имя файла workflow, имя шага, текст run:) для каждого шага с run:.
    uses:-шаги (actions/checkout и т.п.) не несут shell-команд — пропускаются.
    `.yml` И `.yaml` — GitHub Actions грузит оба (находка AI-ревью #146),
    класс односуффиксного скана каталога workflow держит гвардия
    scripts/lib/workflow_glob_suffix_guard.py; тот же приём, что
    scripts/lib/collect_labels.py::_scan_workflow_files."""
    steps = []
    for path in sorted(list(workflows_dir.glob("*.yml")) + list(workflows_dir.glob("*.yaml"))):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for job in (doc.get("jobs") or {}).values():
            for step in job.get("steps") or []:
                run_text = step.get("run")
                if isinstance(run_text, str):
                    steps.append((path.name, step.get("name", "(без имени)"), run_text))
    return steps


def check_exec_bit(steps: list[tuple[str, str, str]], modes: dict[str, str]) -> list[dict]:
    """Чистая проверка: modes — уже загруженная карта `путь -> git-режим`
    (см. git_blob_modes). Путь без записи в modes — не отслеженный git файл
    (например переменная окружения, ошибочно принятая за путь) — пропускается
    молча: это не наш класс, ложных срабатываний из "непонятно что" не заводим.
    Путь с режимом, отличным от 100755 — нарушение."""
    violations = []
    for workflow, step_name, run_text in steps:
        for token in direct_script_invocations(run_text):
            mode = modes.get(token)
            if mode is None:
                continue
            if mode != "100755":
                violations.append({
                    "workflow": workflow,
                    "step": step_name,
                    "path": token,
                    "mode": mode,
                })
    return violations


def git_blob_modes(repo_root: Path = REPO_ROOT, ref: str = "HEAD") -> dict[str, str]:
    """Один вызов `git ls-tree -r` на всё дерево `scripts/` — 0 запросов к
    GitHub API, локальный git уже доступен в любом job'е с checkout."""
    result = subprocess.run(
        ["git", "ls-tree", "-r", ref, "--", "scripts"],
        cwd=repo_root, capture_output=True, text=True, encoding="utf-8", check=True,
    )
    modes: dict[str, str] = {}
    for line in result.stdout.splitlines():
        # формат: "<mode> blob <sha>\t<path>"
        meta, path = line.split("\t", 1)
        mode = meta.split()[0]
        modes[path] = mode
    return modes


def build_report(workflows_dir: Path = WORKFLOWS_DIR, repo_root: Path = REPO_ROOT) -> list[dict]:
    steps = iter_workflow_run_steps(workflows_dir)
    modes = git_blob_modes(repo_root)
    return check_exec_bit(steps, modes)


def main() -> int:
    violations = build_report()
    if not violations:
        print("exec-bit: все скрипты, вызываемые из workflow напрямую, исполняемы")
        return 0
    for item in violations:
        print(
            f"::error::{item['workflow']} [{item['step']}]: "
            f"{item['path']} вызван напрямую без бита исполнения (режим {item['mode']}) — "
            f"chmod +x {item['path']}"
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
