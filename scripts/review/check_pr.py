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

Среда: runner с `gh`, GH_TOKEN с правами pull-requests: write.
"""

import argparse
import json
import os
import re
import subprocess
import sys

LARGE_DIFF_LINES = 800
SECRET_PATTERNS = [
    (r"gh[pousr]_[A-Za-z0-9]{30,}", "GitHub PAT"),
    (r"github_pat_[A-Za-z0-9_]{20,}", "GitHub fine-grained PAT"),
    (r"AKIA[0-9A-Z]{16}", "AWS access key"),
    (r"xox[bposa]-[A-Za-z0-9-]{10,}", "Slack token"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "приватный ключ"),
    (r"(?:TOKEN|SECRET|KEY|PASSWORD|PASSWD)\s*[=:]\s*['\"][A-Za-z0-9+/_-]{20,}['\"]", "литерал секрета в присваивании"),
]
FORBIDDEN_FILES = (".dev.vars", ".env")
REVIEW_OK = "review:ok"
REVIEW_CHANGES = "review:changes-requested"
REVIEW_LARGE = "review:large"


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

    files = gh(f"repos/{repo}/pulls/{args.pr}/files?per_page=100")
    for f in files:
        name = f["filename"]
        if name.endswith(FORBIDDEN_FILES) or name in FORBIDDEN_FILES:
            findings.append(f"Файл секретов в PR: {name}")

    added = sum(f["additions"] for f in files)
    is_large = added > LARGE_DIFF_LINES
    if is_large:
        findings.append(f"Дифф крупный ({added} строк > {LARGE_DIFF_LINES}): авто-слияние запрещено, нужен взгляд человека.")

    # Вердикт-метка: старые вердикты снимаются, вешается актуальный.
    pull = gh(f"repos/{repo}/pulls/{args.pr}")
    current = {label["name"] for label in pull["labels"]}
    for old in (REVIEW_OK, REVIEW_CHANGES):
        if old in current:
            run_gh("api", "-X", "DELETE", f"repos/{repo}/issues/{args.pr}/labels/{old}")
    verdict = REVIEW_LARGE if is_large else (REVIEW_OK if not findings else REVIEW_CHANGES)
    run_gh("api", "-X", "POST", f"repos/{repo}/issues/{args.pr}/labels", "-f", f"labels[]={verdict}")

    if findings:
        body = "Ревью нашло замечания:\n" + "\n".join(f"- {f}" for f in findings)
        run_gh("api", "-X", "POST", f"repos/{repo}/issues/{args.pr}/comments", "-f", body=body)
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
