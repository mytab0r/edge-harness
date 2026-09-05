#!/usr/bin/env python3
"""Что мешает конкретному PR слиться: метки-вердикты, mergeable_state, draft.

Одно место правды гейта слияния — scripts/lib/review_labels.py
(merge_label_gate, CONFLICT_CLEAR_STATES): этот скрипт только читает факты
через gh api и печатает диагноз тем же предикатом, которым пользуется
scheduler.py — не второе, расходящееся определение "что блокирует".

Использование: python3 scripts/gh/pr_blockers.py <номер PR>
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


def gh_api(path: str) -> dict:
    """gh api с автоподбором SOCKS-прокси (грабля #1, docs/agents/INFRA-GH.md).

    Если HTTPS_PROXY уже задан в окружении — используется он, без перебора.
    Иначе пробуются порты по очереди; первый успешный ответ и возвращается.
    """
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
            return json.loads(result.stdout)
        last_err = result.stderr.strip()
    raise SystemExit(
        f"ОШИБКА: gh api {path} не прошёл ни на одном варианте прокси "
        f"({', '.join(str(c) for c in candidates)}): {last_err}"
    )


def main() -> None:
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        raise SystemExit("Использование: python3 scripts/gh/pr_blockers.py <номер PR>")
    pr = sys.argv[1]
    data = gh_api(f"repos/{REPO}/pulls/{pr}")
    labels = [label["name"] for label in data.get("labels", [])]
    state = data.get("mergeable_state")

    print(f"PR #{pr}: {data.get('title')!r}")
    print(f"  state={data.get('state')} draft={data.get('draft')} "
          f"mergeable={data.get('mergeable')} mergeable_state={state}")
    print(f"  labels: {', '.join(labels) or '(нет)'}")

    reasons = []
    if data.get("state") != "open":
        reasons.append(f"PR не открыт (state={data.get('state')}) — оркестратору нечего сливать")
    if data.get("draft"):
        reasons.append("PR в статусе draft — оркестратор его не берёт")
    if review_labels.CONFLICT_LABEL in labels:
        reasons.append("метка conflict — main ушёл вперёд, нужен git rebase origin/main")
    elif state not in (None, "unknown", *review_labels.CONFLICT_CLEAR_STATES):
        reasons.append(f"mergeable_state={state} — GitHub видит проблему слияния помимо меток")
    gate_reason = review_labels.merge_label_gate(labels)
    if gate_reason:
        reasons.append(gate_reason)

    if not reasons:
        print("  Готов к слиянию: оба вердикта зелёные, конфликтов нет — "
              "ждёт своей очереди оркестратора (ровно один PR за прогон).")
    else:
        print("  Блокирует:")
        for reason in reasons:
            print(f"    - {reason}")


if __name__ == "__main__":
    main()
