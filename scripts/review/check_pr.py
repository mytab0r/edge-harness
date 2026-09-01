#!/usr/bin/env python3
"""Детерминированное ревью PR: первый гейт конвейера.

Срабатывает на каждый PR (workflow pr-review). Результат — метка-вердикт,
которую оркестратор использует как условие слияния:
  review:ok                 — замечаний нет, PR может быть слит
  review:changes-requested  — есть находки, автору нужно доработать

Проверки:
  1. Секреты в добавленных строках диффа (репозиторий публичный!): PAT GitHub,
     AWS, Slack, приватные ключи, присваивания длинных литералов *TOKEN/SECRET/KEY.
  2. В PR не добавлены файлы секретов (.dev.vars, .env).
  3. Крупный дифф (> LARGE_DIFF_LINES) помечается меткой review:large — авто-слияние
     для него запрещено, нужен взгляд человека.
  4. Каждый запуск снимает ai:*-метки второго гейта (AI-ревью, #18): вердикт AI
     действителен только для head, на котором сделан, а этот скрипт выполняется
     на каждый пуш. Свежую метку поставит новое AI-ревью (workflow ai-review).

Среда: runner с `gh`, GH_TOKEN с правами pull-requests: write.
"""

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# Метки-вердикты обоих гейтов — одно место правды в scripts/lib
# (общее для check_pr, ai_review и scheduler: имена и гейт слияния).
_LIB = Path(__file__).resolve().parents[1] / "lib" / "review_labels.py"
_spec = importlib.util.spec_from_file_location("review_labels", _LIB)
review_labels = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(review_labels)

LARGE_DIFF_LINES = 800
SECRET_PATTERNS = [
    (r"gh[pousr]_[A-Za-z0-9]{30,}", "GitHub PAT"),
    (r"github_pat_[A-Za-z0-9_]{20,}", "GitHub fine-grained PAT"),
    (r"AKIA[0-9A-Z]{16}", "AWS access key"),
    (r"xox[bposa]-[A-Za-z0-9-]{10,}", "Slack token"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "приватный ключ"),
    (r"(?:TOKEN|SECRET|KEY|PASSWORD|PASSWD)\s*[=:]\s*['\"][A-Za-z0-9+/_-]{20,}['\"]", "литерал секрета в присваивании"),
]
CONFLICT_MARKER = re.compile(r"^(<{7}|={7}|>{7})($| )")
FORBIDDEN_FILES = (".dev.vars", ".env")
REVIEW_OK = review_labels.REVIEW_OK
REVIEW_CHANGES = review_labels.REVIEW_CHANGES
REVIEW_LARGE = review_labels.REVIEW_LARGE
LARGE_OK = review_labels.LARGE_OK


def gh(*args: str) -> dict | list:
    result = subprocess.run(
        ["gh", "api", *args],
        capture_output=True, text=True,
        env={**os.environ, "NO_COLOR": "1"},
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh api {' '.join(args[:2])}: {result.stderr.strip()}")
    return json.loads(result.stdout)


def run_gh(*args: str) -> None:
    result = subprocess.run(
        ["gh", *args],
        capture_output=True, text=True,
        env={**os.environ, "NO_COLOR": "1"},
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:2])}: {result.stderr.strip()}")


def added_lines(diff: str) -> list[str]:
    return [line[1:] for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++")]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--tree", default=".",
                        help="каталог дерева PR для компиляционной проверки "
                             "(workflow pr-review кладёт его в pr-tree; сам "
                             "скрипт исполняется из чекаута main — дерево "
                             "данные, не код)")
    args = parser.parse_args()
    repo = os.environ["GITHUB_REPOSITORY"]

    diff = subprocess.run(
        ["gh", "pr", "diff", str(args.pr)],
        capture_output=True, text=True,
        env={**os.environ, "NO_COLOR": "1"},
    ).stdout

    findings: list[str] = []

    for i, line in enumerate(added_lines(diff)):
        for pattern, kind in SECRET_PATTERNS:
            if re.search(pattern, line):
                findings.append(f"Похоже на {kind} в добавленной строке {i + 1}: «{line.strip()[:60]}…»")
                break
        if CONFLICT_MARKER.match(line):
            findings.append(f"Неразрешённый конфликт-маркер уехал в коммит (строка {i + 1}).")

    files = gh(f"repos/{repo}/pulls/{args.pr}/files?per_page=100")
    for f in files:
        name = f["filename"]
        if name.endswith(FORBIDDEN_FILES) or name in FORBIDDEN_FILES:
            findings.append(f"Файл секретов в PR: {name}")

    added = sum(f["additions"] for f in files)
    # Размерный гейт (> LARGE_DIFF_LINES): авто-слияние запрещено, нужен взгляд
    # человека. По политике 2026-08-30 «взгляд человека» делегирован ревью-агентам
    # и главному агенту: их вердикт публикуется в PR, после чего размер принимается
    # меткой review:large-ok — видимый в истории осознанный обход (паттерн
    # orchestra:skip). Остальные проверки метка не отключает.
    pull = gh(f"repos/{repo}/pulls/{args.pr}")
    current = {label["name"] for label in pull["labels"]}
    size_overflow = added > LARGE_DIFF_LINES
    is_large = size_overflow and LARGE_OK not in current
    if size_overflow and not is_large:
        print(f"review: крупный дифф (+{added}) принят меткой {LARGE_OK}")

    # Каждый изменённый .py обязан компилироваться: ловит обрезанные файлы и
    # неразрешённые конфликты, которые ломают скрипты молча (случалось с scheduler.py).
    # Компиляция — парс, не исполнение: дерево PR остаётся данными. Файла может
    # не быть в чекауте дерева (удалён в PR) — это не ошибка компиляции.
    import py_compile
    for f in files:
        name = f["filename"]
        local = os.path.join(args.tree, name)
        if name.endswith(".py") and name.startswith("scripts/") and os.path.exists(local):
            try:
                py_compile.compile(local, doraise=True)
            except py_compile.PyCompileError as error:
                findings.append(f"{name} не компилируется: {error.msg}")

    # Вердикт-метка: старые вердикты снимаются, вешается актуальный.
    for old in (REVIEW_OK, REVIEW_CHANGES):
        if old in current:
            run_gh("api", "-X", "DELETE", f"repos/{repo}/issues/{args.pr}/labels/{old}")
    # Вердикт AI-ревью (второй гейт, #18) привязан к head, который ревьюили:
    # этот скрипт выполняется на каждый пуш, и обязан снять протухший ai:*
    # ДО нового AI-ревью — иначе оркестратор может слить PR по метке от
    # старого head. Новое ревью поставит свежую метку на новый head.
    for old in review_labels.ai_verdicts_to_drop(current):
        run_gh("api", "-X", "DELETE", f"repos/{repo}/issues/{args.pr}/labels/{old}")
    verdict = REVIEW_LARGE if is_large else (REVIEW_OK if not findings else REVIEW_CHANGES)
    run_gh("api", "-X", "POST", f"repos/{repo}/issues/{args.pr}/labels", "-f", f"labels[]={verdict}")

    if findings:
        body = "Ревью нашло замечания:\n" + "\n".join(f"- {f}" for f in findings)
        run_gh("api", "-X", "POST", f"repos/{repo}/issues/{args.pr}/comments", "-f", f"body={body}")
        for f in findings:
            print(f"::error::{f}")
        print(f"review: FAIL ({verdict})")
        return 1

    print(f"review: OK ({verdict}, +{added} строк)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as error:
        print(f"::error::review: {error}")
        sys.exit(1)
