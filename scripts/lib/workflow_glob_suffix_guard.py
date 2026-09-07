#!/usr/bin/env python3
"""Гвардия класса «скан каталога `.github/workflows` по одному суффиксу»
(issue #635 — класс уже находило ревью дважды и оставался живым).

GitHub Actions грузит workflow и из `*.yml`, и из `*.yaml` — урок был записан
КОММЕНТАРИЕМ (`scripts/lib/test_dispatch_token_usage.py:35-36`: «GitHub Actions
грузит workflows из .yml И .yaml — гвардия обязана видеть оба (находка
AI-ревью #146: файл evil.yaml со скваттером проходил все тесты зелёно)»), а не
гвардией — комментарий читает только тот, кто уже открыл этот файл. На момент
заведения задачи, зелёными в CI, живут ТРИ независимых places с ровно этим
дефектом:

  - scripts/lib/exec_bit_guard.py:120 (`workflows_dir.glob("*.yml")`, внутри
    `iter_workflow_run_steps`);
  - scripts/lib/orphan_test_guard.py:161 (тот же вызов, та же функция);
  - scripts/lib/test_pr_body_label_release_reachable.py:169
    (`WORKFLOWS_DIR.glob("*.yml")` на верхнем уровне модуля).

Рядом с ними в том же репозитории уже лежат ДВА принятых образца починки
того же класса — они и задают стиль требуемого фикса, не эта гвардия:

  - scripts/lib/collect_labels.py:112 — union двух вызовов
    `list(glob("*.yml")) + list(glob("*.yaml"))` в одной функции;
  - scripts/lib/test_infra_gh_inventory.py:41 — один вызов
    `glob("*.y*ml")`, комбинированный паттерн покрывает оба расширения сразу.

Устройство — семантический разбор `ast`, а не текстовый grep по подстроке
`"*.yml"`: переформулировка вида `glob('*' + '.yml')` или чтение паттерна из
переменной обошла бы grep, но не эту гвардию ровно там, где она это заявляет
(см. честный потолок ниже — резолвер тоже не всемогущ).

Шаг 1 (`workflow_dir_names`). Резолвит имена переменных/параметров, которые
СЕМАНТИЧЕСКИ ссылаются на каталог `.github/workflows`:
  а) присваивание на любом уровне модуля, чьё значение — цепочка pathlib
     `a / b / c` (`ast.BinOp` с `ast.Div`), последние два компонента —
     строковые константы `".github"` и `"workflows"` в этом порядке —
     `WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"` ловится ровно
     тем способом, каким этот путь и правда построен во всех пяти файлах
     выше;
  б) алиасы (`X = Y`, Y уже в множестве) и параметры функций со значением
     по умолчанию (`def f(workflows_dir: Path = WORKFLOWS_DIR)`) — ровно
     паттерн, которым пользуются exec_bit_guard.py/orphan_test_guard.py.
     Резолвер — фиксированная точка (порядок объявлений в исходнике не
     гарантирован).

Честный потолок резолвера: он НЕ трассирует произвольные выражения —
`Path(str(...))`, f-строки, `os.path.join`, конкатенацию через `+`. Он также
не ловит скан через `Path.iterdir()` с ручным фильтром по `.suffix` (другая
форма того же по духу класса) — на 2026-09-07 в репозитории единственный
живой пример этой формы (`test_dispatch_token_usage.py:94-97`) уже корректен
(`path.suffix in {".yml", ".yaml"}`), расширять резолвер на неё сейчас —
спекуляция без второго нарушителя. Новая форма построения пути или новый
нарушитель через `iterdir()` — повод расширить эту гвардию, а не повод считать
её недостижимой (тот же честный потолок, каким `exec_bit_guard.py` объявляет
свой `SCANNED_PREFIXES`).

Шаг 2 (`find_glob_calls`). Обходит все `ast.Call`, где `func` — `Attribute` с
`attr` в `{"glob", "rglob"}`, а объект вызова (`Name`, либо хвост `Attribute`
вида `self.workflows_dir`) — имя из шага 1, с константным строковым первым
аргументом. Каждый вызов относится к охватывающей `FunctionDef`/`AsyncFunctionDef`
(или к модулю, если вызов вне функции) — та же гранулярность, на которой
`collect_labels.py::_scan_workflow_files` объединяет два вызова в одной
функции.

Шаг 3 (`find_violations`). Внутри каждой такой области видимости все найденные
паттерны, упоминающие `yml`/`yaml` (иначе это вообще не про класс YAML-
суффиксов — например `.glob("*.json")` в workflow-каталоге не наш случай),
проверяются `fnmatch` на двух пробных именах `probe.yml`/`probe.yaml`:
покрытие есть, если хотя бы один паттерн (не обязательно один и тот же для
обоих проб) матчит `probe.yml`, И хотя бы один матчит `probe.yaml`. Так
`"*.y*ml"` (test_infra_gh_inventory.py) проходит одним вызовом, `"*.yml"` +
`"*.yaml"` в одной области (collect_labels.py) проходят союзом двух вызовов, а
одинокий `"*.yml"` (три места выше) — нарушение.

Газ (правило «тормоз без газа», AGENTS.md) — тот же анкер на начало строки,
что `orphan_test_guard.py::EXEMPTION_RE`: строка `# WORKFLOW-DIR-GLOB-OK:
<причина>` где-либо в файле снимает нарушения ЭТОГО файла целиком. Пустая
причина не считается газом (регэксп требует непустой хвост) — тормоз без
названного условия возврата этой гвардией не принимается.

Догфудинг (требование «не быть нарушителем собственного правила»): сама эта
гвардия живёт под `scripts/**/*.py` — тем же деревом, которое сканирует —
поэтому если бы её исходник когда-нибудь читал `.github/workflows` напрямую,
он обязан делать это тем же безопасным способом. `workflow_dir_has_files`
ниже — единственное место в этом модуле, которое трогает реальный каталог
`.github/workflows`, и оно сканирует его по ОБОИМ суффиксам (`"*.y*ml"`),
не одному — используется как санитарная проверка входа (шаг ниже), а не
только как декларация принципа.

При нуле найденных python-кандидатов для анализа (`scripts/**/*.py` пуст —
верный признак того, что `scripts_dir` указывает не туда) либо при нуле
файлов в `.github/workflows` (признак того, что `workflows_dir` указывает не
туда) — `build_report` падает громко (`RuntimeError`), а не молча печатает
«нарушений нет» по пустому множеству кандидатов (тот же класс «тихий ноль»,
что и в остальной части этого поручения).

Запуск:
  python scripts/lib/workflow_glob_suffix_guard.py    # печать отчёта, exit 1 при нарушении
  python -m pytest scripts/lib/test_workflow_glob_suffix_guard.py -q
"""

from __future__ import annotations

import ast
import fnmatch
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

# Анкер на начало строки (после пробелов и `#`) — не голая подстрока где
# угодно в файле: без анкера собственный тест этой гвардии, несущий литерал
# маркера как фикстуру, ложно засчитывал бы себя в газ (та же находка, что
# уже документирована в orphan_test_guard.py::EXEMPTION_RE).
EXEMPTION_RE = re.compile(r"^\s*#\s*WORKFLOW-DIR-GLOB-OK:\s*(\S.*\S|\S)\s*$", re.MULTILINE)

# Пробные имена для fnmatch-проверки покрытия — сами по себе не значимы,
# важны только расширения.
_PROBE_YML = "probe.yml"
_PROBE_YAML = "probe.yaml"

_GLOB_METHODS = ("glob", "rglob")


def _div_chain(node: ast.AST) -> list[ast.AST]:
    """Разворачивает цепочку pathlib `a / b / c` (левоассоциативный `ast.BinOp`
    с `ast.Div`) в плоский список операндов слева направо."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _div_chain(node.left) + [node.right]
    return [node]


def _is_workflows_dir_expr(node: ast.AST) -> bool:
    """True, если выражение — pathlib-цепочка, чьи последние два компонента —
    строковые константы `".github"` и `"workflows"` в этом порядке."""
    parts = _div_chain(node)
    if len(parts) < 2:
        return False
    tail_values: list[str] = []
    for part in parts[-2:]:
        if isinstance(part, ast.Constant) and isinstance(part.value, str):
            tail_values.append(part.value)
        else:
            return False
    return tail_values == [".github", "workflows"]


def _assign_targets(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Assign):
        return [t.id for t in node.targets if isinstance(t, ast.Name)]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return [node.target.id]
    return []


def workflow_dir_names(tree: ast.AST) -> set[str]:
    """Множество имён переменных/параметров, семантически ссылающихся на
    `.github/workflows` — см. докстринг модуля, шаг 1."""
    names: set[str] = set()

    assigns: list[tuple[list[str], ast.AST]] = []
    param_defaults: list[tuple[str, ast.AST]] = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = _assign_targets(node)
            if targets:
                assigns.append((targets, node.value))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            positional = node.args.args
            defaults = node.args.defaults
            if defaults:
                paired = zip(positional[len(positional) - len(defaults):], defaults)
                for arg, default in paired:
                    param_defaults.append((arg.arg, default))

    changed = True
    while changed:
        changed = False
        for targets, value in assigns:
            is_direct = _is_workflows_dir_expr(value)
            is_alias = isinstance(value, ast.Name) and value.id in names
            if is_direct or is_alias:
                for target in targets:
                    if target not in names:
                        names.add(target)
                        changed = True
        for arg_name, default in param_defaults:
            if isinstance(default, ast.Name) and default.id in names and arg_name not in names:
                names.add(arg_name)
                changed = True

    return names


def _call_object_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _scope_label(function_stack: list[ast.AST]) -> str:
    if function_stack:
        return function_stack[-1].name
    return "<module>"


def find_glob_calls(tree: ast.AST, names: set[str]) -> list[dict]:
    """(scope, pattern, method, lineno) для каждого `.glob`/`.rglob` на
    известное имя каталога workflow с константным строковым первым
    аргументом — см. докстринг модуля, шаг 2."""
    results: list[dict] = []
    stack: list[ast.AST] = []

    class Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            stack.append(node)
            self.generic_visit(node)
            stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in _GLOB_METHODS
                and _call_object_name(func.value) in names
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                results.append({
                    "scope": _scope_label(stack),
                    "pattern": node.args[0].value,
                    "method": func.attr,
                    "lineno": node.lineno,
                })
            self.generic_visit(node)

    Visitor().visit(tree)
    return results


def _mentions_yaml_family(pattern: str) -> bool:
    return "yml" in pattern or "yaml" in pattern


def _covers_both_suffixes(patterns: list[str]) -> bool:
    covers_yml = any(fnmatch.fnmatch(_PROBE_YML, p) for p in patterns)
    covers_yaml = any(fnmatch.fnmatch(_PROBE_YAML, p) for p in patterns)
    return covers_yml and covers_yaml


def find_violations(source: str, filename: str = "<string>") -> list[dict]:
    """Сырые находки (без учёта газа) для одного файла — см. докстринг
    модуля, шаг 3. `filename` только для сообщений об ошибках `ast.parse`."""
    tree = ast.parse(source, filename=filename)
    names = workflow_dir_names(tree)
    if not names:
        return []
    calls = find_glob_calls(tree, names)
    if not calls:
        return []

    by_scope: dict[str, list[dict]] = {}
    for call in calls:
        by_scope.setdefault(call["scope"], []).append(call)

    violations: list[dict] = []
    for scope, scoped_calls in by_scope.items():
        patterns = [c["pattern"] for c in scoped_calls]
        if not any(_mentions_yaml_family(p) for p in patterns):
            continue  # не про yml/yaml вовсе — не наш класс
        if _covers_both_suffixes(patterns):
            continue
        for call in scoped_calls:
            violations.append({
                "scope": scope,
                "pattern": call["pattern"],
                "method": call["method"],
                "lineno": call["lineno"],
            })
    return violations


def read_exemption(text: str) -> str | None:
    """Газ: `# WORKFLOW-DIR-GLOB-OK: <причина>`, начинающая строку файла.
    Пустая причина — не газ."""
    match = EXEMPTION_RE.search(text)
    return match.group(1).strip() if match else None


def workflow_dir_has_files(workflows_dir: Path = WORKFLOWS_DIR) -> bool:
    """Дискавери самого каталога `.github/workflows` ОБОИМИ суффиксами разом
    (`"*.y*ml"`, тот же приём, что test_infra_gh_inventory.py:41) — единственное
    место в этом модуле, которое реально читает каталог workflow, и оно обязано
    не быть нарушителем собственного правила (см. докстринг модуля,
    «Догфудинг»)."""
    return any(workflows_dir.glob("*.y*ml"))


def build_report(scripts_dir: Path = SCRIPTS_DIR, workflows_dir: Path = WORKFLOWS_DIR) -> dict:
    py_files = sorted(scripts_dir.rglob("*.py"))
    if not py_files:
        raise RuntimeError(
            f"workflow-glob-suffix-guard: 0 python-файлов под {scripts_dir} — "
            "каталог кандидатов пуст, это не «нарушений нет», это неверный "
            "путь (fail loud вместо тихого нуля)"
        )
    if not workflow_dir_has_files(workflows_dir):
        raise RuntimeError(
            f"workflow-glob-suffix-guard: 0 файлов *.yml/*.yaml в {workflows_dir} — "
            "каталог workflow пуст или путь неверен (fail loud вместо тихого нуля)"
        )

    violations: list[dict] = []
    exemptions: dict[str, str] = {}
    for path in py_files:
        text = path.read_text(encoding="utf-8")
        # Относительно родителя `scripts_dir`, не жёстко REPO_ROOT: реальный
        # прогон (`scripts_dir=SCRIPTS_DIR=REPO_ROOT/"scripts"`) даёт
        # repo-relative путь вида `scripts/lib/x.py`; тестовый прогон на
        # временном дереве вне REPO_ROOT (другой диск на Windows) не падает
        # `ValueError` от `Path.relative_to`, как падал бы жёсткий REPO_ROOT.
        rel = path.relative_to(scripts_dir.parent).as_posix()
        try:
            raw = find_violations(text, filename=rel)
        except SyntaxError as exc:
            raise RuntimeError(f"workflow-glob-suffix-guard: {rel} не парсится как Python: {exc}") from exc
        if not raw:
            continue
        reason = read_exemption(text)
        if reason:
            exemptions[rel] = reason
            continue
        for item in raw:
            violations.append({**item, "path": rel})

    return {
        "scanned": len(py_files),
        "violations": violations,
        "exemptions": exemptions,
    }


def main() -> int:
    try:
        report = build_report()
    except RuntimeError as exc:
        print(f"::error::{exc}")
        return 1

    print(
        f"workflow-glob-suffix-guard: python-файлов просканировано {report['scanned']}, "
        f"газ (осознанные исключения) {len(report['exemptions'])}"
    )
    for path, reason in sorted(report["exemptions"].items()):
        print(f"  газ: {path} — {reason}")

    if not report["violations"]:
        print("workflow-glob-suffix-guard: односуффиксных сканов каталога workflow не найдено")
        return 0

    for item in sorted(report["violations"], key=lambda v: (v["path"], v["lineno"])):
        print(
            f"::error::{item['path']}:{item['lineno']} [{item['scope']}]: "
            f".{item['method']}(\"{item['pattern']}\") сканирует .github/workflows "
            "только одним суффиксом — GitHub Actions грузит и *.yml, и *.yaml. "
            "Объедини два вызова (scripts/lib/collect_labels.py:112) или используй "
            "комбинированный паттерн '*.y*ml' (scripts/lib/test_infra_gh_inventory.py:41), "
            "либо, если это осознанное исключение, добавь в файл "
            "'# WORKFLOW-DIR-GLOB-OK: <причина>'."
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
