#!/usr/bin/env python3
"""Канарейка класса «осиротевший тест» (#583).

Класс: PR #568 слил `scripts/lib/test_duplicate_guard.py` (семь тестов гвардии
дублей issue) — файл не был подключён ни к одному workflow, ни разу не
исполнялся. Вскрылось не проверкой, а ручным чтением. Сплошной прочёс
2026-09-07 нашёл ещё девять таких же файлов (см. docstring test-модуля этой
гвардии — точный список).

Устройство:
  1. `discover_test_files` — файловая система, не список в коде. Два правила,
     покрывающие ВСЕ виды тестов, реально живущие в репозитории (проверено
     грепом на 2026-09-07, не додумано): (а) python `test_*.py`/`*_test.py`
     где угодно в дереве; (б) любой файл с известным тестовым расширением,
     лежащий НЕПОСРЕДСТВЕННО в директории с именем `test` (bash `*.test.sh`,
     `*.smoke.sh`, `*.guard.sh` — три варианта именования одного и того же
     класса, живущие в scripts/*/test/; node `*.test.mjs`; vitest `*.spec.ts`).
  2. `iter_workflow_run_steps` — реальные шаги `run:` из `.github/workflows/*.yml`
     (yaml.safe_load, не текстовый греп по имени файла).
  3. `build_coverage` — разбирает КОМАНДЫ этих шагов (pytest/node --test/
     npm test/bash/прямой вызов), а не ищет подстроку имени файла: команда
     `pytest scripts/lib/` покрывает ЛЮБОЙ python-тест под этой директорией,
     `npm test` в cf-worker (vitest, дефолтный include-glob, без переопределения
     в vitest.config.ts) покрывает ЛЮБОЙ `*.spec.ts`/`*.test.ts` под cf-worker.
  4. Легальный газ — маркер `ORPHAN-TEST-OK: <причина>` в самом файле теста
     (`read_exemption`). Молчаливого исключения нет: пустая причина не считается
     газом (регэксп требует непустой хвост).
  5. `build_report` сводит 1-4 в список нарушений с точным текстом (какой файл,
     что сделать) — красный exit code при непустом списке.

Честный потолок: `node --test <dir>` без явного имени файла в этом
репозитории сейчас не встречается — классификатор различает файл/директорию
по наличию точки-расширения в последнем сегменте (эвристика, не полный
парсер), тот же приём использует exec_bit_guard.py для похожей задачи.

Запуск:
  python scripts/lib/orphan_test_guard.py       # печать отчёта, exit 1 при находках
  python -m pytest scripts/lib/test_orphan_test_guard.py -q
"""

from __future__ import annotations

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import os
import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

# Каталог гвардий (#749): тесты, запускаемые ОПОСРЕДОВАННО через перебор
# scripts/ci/run_guards.sh, а не напрямую из .github/workflows/*.yml —
# миграция гвардии в каталог не должна порождать пачку осиротевших тестов
# (см. iter_guard_catalog_steps/_catalog_runner_is_wired ниже).
GUARD_CATALOG_DIR = REPO_ROOT / "scripts" / "ci" / "guards"
GUARD_CATALOG_RUNNER = "scripts/ci/run_guards.sh"

# Только этот workflow+job засчитывает подключение перебора (ревью PR #771,
# minor 7): required-проверка ветки main — job `test` файла `repo-ci.yml`
# (см. комментарий вверху repo-ci.yml, «Job id обязан быть ровно test»).
# Вызов run_guards.sh из ЛЮБОГО другого workflow/job (например
# необязательного deploy-dsh-edge.yml с continue-on-error) не должен
# засчитываться как «каталог подключён» — иначе комбинированная мутация
# «убрать шаг-перебор из repo-ci.yml, добавить в необязательный workflow»
# проходила бы канарейку молча, при том что required-гейт каталог вообще не
# исполняет.
GUARD_CATALOG_REQUIRED_WORKFLOW = "repo-ci.yml"
GUARD_CATALOG_REQUIRED_JOB = "test"

EXCLUDED_DIR_NAMES = {".git", "node_modules", "dist", ".claude", ".githooks"}

# Расширения, которые реально встречаются в директориях `test/` этого
# репозитория (см. докстринг модуля) — файл с другим расширением в такой
# директории (например README) тестом не считается.
TEST_DIR_EXTENSIONS = {".sh", ".mjs", ".js", ".cjs", ".ts", ".tsx"}

_PY_TEST_RE = re.compile(r"^(test_.+|.+_test)\.py$")

# Анкер на НАЧАЛО строки (после пробелов и символа комментария) — не голая
# подстрока где угодно в файле: без анкера собственный тест этой канарейки
# (test_orphan_test_guard.py) ложно засчитывал себя в газ, потому что несёт
# ЛИТЕРАЛ строки маркера как тестовую фикстуру (`write_text("# ORPHAN-TEST-OK:
# ...")`) — такая строка исходника не начинается с `#`, поэтому анкер её не
# путает с настоящим объявлением. Живая находка при первом прогоне канарейки
# на самой себе (см. test_own_marker_literal_is_not_self_exempting ниже).
EXEMPTION_RE = re.compile(r"^\s*(?:#+|//+|\*+)\s*ORPHAN-TEST-OK:\s*(\S.*\S|\S)\s*$", re.MULTILINE)

# Имя файла в соглашении test/spec (node --test, vitest) — тот же дефолтный
# include-glob, что понимает vitest без переопределения в vitest.config.ts
# (`**/*.{test,spec}.?(c|m)[jt]s?(x)`).
_JS_TEST_NAME_RE = re.compile(r"\.(test|spec)\.[cm]?[jt]sx?$")


def _relpath(path: Path, root: Path = REPO_ROOT) -> str:
    return path.relative_to(root).as_posix()


def discover_test_files(repo_root: Path = REPO_ROOT) -> list[Path]:
    """Файловая система — не список в коде (требование #583)."""
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIR_NAMES]
        base = Path(dirpath)
        in_test_dir = base.name == "test"
        for name in filenames:
            if _PY_TEST_RE.match(name):
                found.append(base / name)
                continue
            if in_test_dir and Path(name).suffix in TEST_DIR_EXTENSIONS:
                found.append(base / name)
    return sorted(found, key=lambda p: _relpath(p, repo_root))


def read_exemption(path: Path) -> str | None:
    """Газ (#583, требование 4): строка вида `# ORPHAN-TEST-OK: <причина>`
    (или `//`/`*` для JS/TS-комментариев), начинающая строку файла — не
    подстрока где угодно (см. EXEMPTION_RE). Пустая/отсутствующая причина —
    не газ (regex требует непустой хвост после двоеточия)."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    match = EXEMPTION_RE.search(text)
    return match.group(1).strip() if match else None


# ── Разбор run:-текста workflow (тот же приём, что exec_bit_guard.py) ──────

_STATEMENT_SPLIT_RE = re.compile(r"&&|\|\||[;|\n`]|\$\(|(?<!\\)\(")
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*$")
_HEREDOC_START_RE = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?")


def _strip_heredocs(text: str) -> str:
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
        i += 1
    return "\n".join(out_lines)


def _strip_comments(text: str) -> str:
    return "\n".join(re.sub(r"(^|\s)#.*$", "", line) for line in text.split("\n"))


def statement_tokens(run_text: str) -> list[list[str]]:
    """Каждый shell-statement run-блока как список слов, ведущие присваивания
    `VAR=value` пропущены (может быть несколько подряд)."""
    text = _strip_comments(_strip_heredocs(run_text))
    statements = []
    for part in _STATEMENT_SPLIT_RE.split(text):
        words = [w.strip("()\"'`") for w in part.split()]
        idx = 0
        while idx < len(words) and _ASSIGNMENT_RE.match(words[idx]):
            idx += 1
        words = words[idx:]
        if words:
            statements.append(words)
    return statements


def iter_workflow_run_steps(workflows_dir: Path = WORKFLOWS_DIR) -> list[dict]:
    """IO: по одному элементу на каждый шаг с `run:` — (workflow, job, step,
    cwd, run_text). `cwd` — working-directory шага/джоба/файла (без
    подстановки динамических `${{ }}`-выражений: такие пути не резолвятся в
    файл репозитория, поэтому просто не участвуют в покрытии — это безопасная
    сторона ошибки, ложноположительных «покрыт» она не даёт).
    `.yml` И `.yaml` (GitHub Actions грузит оба, находка AI-ревью #146,
    класс держит гвардия scripts/lib/workflow_glob_suffix_guard.py) — тот же
    приём, что scripts/lib/collect_labels.py::_scan_workflow_files."""
    steps = []
    for path in sorted(list(workflows_dir.glob("*.yml")) + list(workflows_dir.glob("*.yaml"))):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        wf_default_cwd = (
            ((doc.get("defaults") or {}).get("run") or {}).get("working-directory")
        )
        for job_id, job in (doc.get("jobs") or {}).items():
            job_default_cwd = (
                ((job.get("defaults") or {}).get("run") or {}).get("working-directory")
            )
            cwd = job_default_cwd or wf_default_cwd
            for step in job.get("steps") or []:
                run_text = step.get("run")
                if not isinstance(run_text, str):
                    continue
                step_cwd = step.get("working-directory") or cwd
                steps.append({
                    "workflow": path.name,
                    "job": job_id,
                    "step": step.get("name", "(без имени)"),
                    "cwd": step_cwd,
                    "run": run_text,
                })
    return steps


def _catalog_runner_is_wired(steps: list[dict]) -> bool:
    """Каталог гвардий засчитывается в покрытие, только если
    `scripts/ci/run_guards.sh` реально вызван шагом ИМЕННО job
    `GUARD_CATALOG_REQUIRED_JOB` файла `GUARD_CATALOG_REQUIRED_WORKFLOW` —
    иначе файлы каталога не подключены к required-CI вовсе, и притворяться,
    что они покрыты, было бы ложным зелёным (#749, тот же класс, который
    сама канарейка ловит наоборот: «файл лежит, но не запускается»).
    Вызов из другого workflow/job (ревью PR #771, minor 7) не засчитывается —
    комбинированная мутация «убрать шаг из repo-ci.yml, добавить в
    необязательный workflow с continue-on-error» проходила бы это раньше."""
    for step in steps:
        if (
            step.get("workflow") != GUARD_CATALOG_REQUIRED_WORKFLOW
            or step.get("job") != GUARD_CATALOG_REQUIRED_JOB
        ):
            continue
        for words in statement_tokens(step["run"]):
            if not words:
                continue
            if words[0] in ("bash", "sh") and len(words) >= 2 and words[1] == GUARD_CATALOG_RUNNER:
                return True
            if words[0] == GUARD_CATALOG_RUNNER:
                return True
    return False


def iter_guard_catalog_steps(catalog_dir: Path = GUARD_CATALOG_DIR) -> list[dict]:
    """Каждый файл `scripts/ci/guards/*.sh` — псевдо-шаг с его содержимым как
    run-текст (#749): перебор каталога исполняет их так же, как раньше
    исполнял рукописный шаг repo-ci.yml, поэтому канарейка обязана видеть
    команды pytest/node --test внутри них, не только внутри
    `.github/workflows/*.yml`."""
    steps: list[dict] = []
    if not catalog_dir.is_dir():
        return steps
    for path in sorted(catalog_dir.glob("*.sh")):
        steps.append({
            "workflow": "scripts/ci/guards/" + path.name,
            "job": "guard-catalog",
            "step": path.name,
            "cwd": None,
            "run": path.read_text(encoding="utf-8", errors="ignore"),
        })
    return steps


def _resolve(cwd: str | None, arg: str) -> str | None:
    """Путь `arg`, разрешённый относительно `cwd` (оба — repo-relative posix),
    в виде repo-relative posix-строки. `None`, если `cwd` содержит
    нерезолвимое `${{ }}`-выражение (динамический путь GitHub Actions)."""
    if cwd and "${{" in cwd:
        return None
    base = Path(cwd) if cwd else Path(".")
    try:
        combined = (base / arg)
    except (TypeError, ValueError):
        return None
    parts: list[str] = []
    for part in combined.parts:
        if part in (".", ""):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


def _is_dir_like(arg: str) -> bool:
    """Эвристика «директория, а не файл»: последний сегмент без точки, или
    явный trailing slash — то же различение, что уже требовалось exec_bit_guard
    (файл против директории по расширению)."""
    if arg.endswith("/"):
        return True
    last = arg.rsplit("/", 1)[-1]
    return "." not in last


def build_coverage(steps: list[dict], test_files: list[str]) -> set[str]:
    """`test_files` — repo-relative posix-пути (из discover_test_files).
    Возвращает подмножество, покрытое реальными командами шагов."""
    covered: set[str] = set()
    py_files = [f for f in test_files if f.endswith(".py")]
    js_test_files = [f for f in test_files if _JS_TEST_NAME_RE.search(f)]
    all_files_set = set(test_files)

    def cover_dir(prefix: str, candidates: list[str]) -> None:
        pre = "" if not prefix else prefix.rstrip("/") + "/"
        for f in candidates:
            if f == prefix or f.startswith(pre):
                covered.add(f)

    for step in steps:
        cwd = step["cwd"]
        for words in statement_tokens(step["run"]):
            head = words[0]

            if head == "pytest" or (
                head in ("python", "python3") and len(words) >= 3 and words[1] == "-m" and words[2] == "pytest"
            ):
                start = 1 if head == "pytest" else 3
                for arg in words[start:]:
                    if arg.startswith("-"):
                        continue
                    resolved = _resolve(cwd, arg)
                    if resolved is None:
                        continue
                    if resolved in all_files_set:
                        covered.add(resolved)
                    elif _is_dir_like(arg):
                        cover_dir(resolved, py_files)

            elif head == "node" and "--test" in words:
                idx = words.index("--test")
                for arg in words[idx + 1:]:
                    if arg.startswith("-"):
                        continue
                    resolved = _resolve(cwd, arg)
                    if resolved is None:
                        continue
                    if resolved in all_files_set:
                        covered.add(resolved)
                    elif _is_dir_like(arg):
                        cover_dir(resolved, js_test_files)

            elif head == "npm" and (
                (len(words) >= 2 and words[1] == "test") or (len(words) >= 3 and words[1] == "run" and words[2] == "test")
            ):
                # `npm test`/`npm run test` — vitest run без переопределения
                # include в vitest.config.ts (см. докстринг модуля): дефолтный
                # glob покрывает любой `*.spec.*`/`*.test.*` под cwd.
                resolved_cwd = _resolve(cwd, ".")
                if resolved_cwd is not None:
                    prefix = "" if not resolved_cwd else resolved_cwd.rstrip("/") + "/"
                    for f in js_test_files:
                        if f == resolved_cwd or f.startswith(prefix):
                            covered.add(f)

            elif head in ("bash", "sh") and len(words) >= 2:
                resolved = _resolve(cwd, words[1])
                if resolved is not None and resolved in all_files_set:
                    covered.add(resolved)

            elif "/" in head and not head.startswith("-"):
                # Прямой вызов скрипта без интерпретатора (класс #510/#516).
                resolved = _resolve(cwd, head)
                if resolved is not None and resolved in all_files_set:
                    covered.add(resolved)

    return covered


def build_report(
    repo_root: Path = REPO_ROOT,
    workflows_dir: Path = WORKFLOWS_DIR,
    catalog_dir: Path | None = None,
) -> dict:
    # `catalog_dir` по умолчанию — `None`, резолвится ОТ `repo_root` вызова
    # (не от захардкоженной константы GUARD_CATALOG_DIR модуля): вызов
    # `build_report(repo_root=tmp_path)` без явного `catalog_dir` иначе тихо
    # читал бы ЖИВОЙ каталог `scripts/ci/guards/` этого репозитория мимо
    # синтетической фикстуры `tmp_path` (ревью PR #771, minor 10).
    if catalog_dir is None:
        catalog_dir = repo_root / "scripts" / "ci" / "guards"
    files = discover_test_files(repo_root)
    rel_files = [_relpath(f, repo_root) for f in files]
    steps = iter_workflow_run_steps(workflows_dir)
    if _catalog_runner_is_wired(steps):
        steps = steps + iter_guard_catalog_steps(catalog_dir)
    covered = build_coverage(steps, rel_files)

    exemptions: dict[str, str] = {}
    for f in files:
        reason = read_exemption(f)
        if reason:
            exemptions[_relpath(f, repo_root)] = reason

    orphans = sorted(
        rel for rel in rel_files
        if rel not in covered and rel not in exemptions
    )
    return {
        "total": len(rel_files),
        "covered": sorted(covered),
        "exemptions": exemptions,
        "orphans": orphans,
    }


def _suggest(path: str) -> str:
    # Ревью PR #771, minor 8: раньше единственная подсказка была «допиши
    # рукописный шаг в repo-ci.yml» — ровно то, за что краснеет гвардия
    # рецидива scripts/lib/ci_guard_registration_guard.py (#749). Каталог
    # scripts/ci/guards/<имя>.sh — равноценная (и предпочтительная для
    # новых проверок) альтернатива, названа явно, а не молчит.
    if path.startswith("cf-worker/"):
        return "добавь в job `worker-test` .github/workflows/worker-ci.yml (npm test уже покрывает cf-worker/test/, проверь, что файл под этой директорией)"
    if path.endswith(".py"):
        return (
            f"добавь шаг `python -m pytest {path} -q` в job `test` "
            ".github/workflows/repo-ci.yml, или файл `scripts/ci/guards/<имя>.sh` "
            "с той же командой внутри (#749) — каталог, не рукописный шаг"
        )
    if path.endswith((".mjs", ".js", ".cjs", ".ts", ".tsx")):
        return (
            f"добавь шаг `node --test {path}` в job `test` "
            ".github/workflows/repo-ci.yml, или файл `scripts/ci/guards/<имя>.sh` "
            "с той же командой внутри (#749) — каталог, не рукописный шаг"
        )
    if path.endswith(".sh"):
        return (
            f"добавь шаг `bash {path}` в job `test` .github/workflows/repo-ci.yml, "
            "или файл `scripts/ci/guards/<имя>.sh` с той же командой внутри (#749) — "
            "каталог, не рукописный шаг"
        )
    return f"подключи {path} к шагу подходящего workflow — сейчас не запускается нигде"


def main() -> int:
    report = build_report()
    print(f"orphan-test-guard: файлов тестов найдено {report['total']}, "
          f"подключено {len(report['covered'])}, "
          f"газ (осознанные исключения) {len(report['exemptions'])}")
    for path, reason in sorted(report["exemptions"].items()):
        print(f"  газ: {path} — {reason}")
    if not report["orphans"]:
        print("orphan-test-guard: осиротевших тестов нет")
        return 0
    for path in report["orphans"]:
        print(f"::error::{path} не запускается ни одним workflow (осиротевший тест) — {_suggest(path)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
