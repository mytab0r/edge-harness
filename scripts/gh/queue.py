#!/usr/bin/env python3
"""Обзор очереди открытых PR: метки-вердикты, mergeable_state, что мешает
слиться каждому. Строка на PR, без похода в отдельный скрипт на каждый номер.

Листает список PR до конца через review_labels.list_pages (класс #308/#310:
сырая первая страница по 100 штук молча теряет хвост при >100 открытых PR/
задач — обход обязателен, не опция). Гейт слияния — то же review_labels.py,
что использует scheduler.py: одно место правды, не пересказ.

Использование: python3 scripts/gh/queue.py
"""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

PROXY_PORTS = (1084, 1083, 1085)
REPO = os.environ.get("GH_REPO", "mytab0r/edge-harness")

_rl_spec = importlib.util.spec_from_file_location(
    "review_labels", Path(__file__).resolve().parents[1] / "lib" / "review_labels.py")
review_labels = importlib.util.module_from_spec(_rl_spec)
_rl_spec.loader.exec_module(review_labels)


def gh(path: str):
    """gh api с автоподбором SOCKS-прокси — та же логика, что в pr_blockers.py
    (два runtime'а, bash и python, не делят процесс — держим обе копии в
    docs/agents/INFRA-GH.md синхронизированными вручную, список портов один)."""
    base_env = {**os.environ, "NO_COLOR": "1"}
    candidates = [base_env.get("HTTPS_PROXY")] if base_env.get("HTTPS_PROXY") else [
        f"socks5://127.0.0.1:{port}" for port in PROXY_PORTS
    ]
    last_err = "(нет попыток)"
    for proxy in candidates:
        run_env = dict(base_env)
        if proxy:
            run_env["HTTPS_PROXY"] = proxy
        result = subprocess.run(
            # encoding="utf-8" явно: gh отдаёт UTF-8 всегда, а text=True без
            # него декодирует кодовой страницей консоли (cp1251 на Windows) —
            # падает на первой не-ASCII строке (заголовок PR, кириллица).
            ["gh", "api", path], capture_output=True, text=True, encoding="utf-8", env=run_env,
        )
        if result.returncode == 0:
            return json.loads(result.stdout) if result.stdout.strip() else None
        last_err = result.stderr.strip()
    raise SystemExit(
        f"ОШИБКА: gh api {path} не прошёл ни на одном варианте прокси "
        f"({', '.join(str(c) for c in candidates)}): {last_err}"
    )


def main() -> None:
    pulls = review_labels.list_pages(f"repos/{REPO}/pulls?state=open&per_page=100", gh)
    if not pulls:
        print("Открытых PR нет.")
        return
    print(f"{len(pulls)} открытых PR:\n")
    for pull in pulls:
        labels = [label["name"] for label in pull.get("labels", [])]
        state = pull.get("mergeable_state")
        if pull.get("draft"):
            verdict = "draft"
        elif review_labels.CONFLICT_LABEL in labels:
            verdict = "conflict — нужен rebase"
        elif state not in (None, "unknown", *review_labels.CONFLICT_CLEAR_STATES):
            verdict = f"mergeable_state={state}"
        else:
            gate_reason = review_labels.merge_label_gate(labels)
            verdict = gate_reason or "готов к слиянию"
        title = pull["title"][:56]
        print(f"#{pull['number']:<5} {title:<58} {','.join(labels) or '-':<32} {verdict}")


if __name__ == "__main__":
    main()
