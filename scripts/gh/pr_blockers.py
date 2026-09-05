#!/usr/bin/env python3
"""Что мешает конкретному PR слиться: метки-вердикты, mergeable_state, draft.

Одно место правды гейта слияния — scripts/lib/review_labels.py
(merge_label_gate, CONFLICT_CLEAR_STATES): этот скрипт только читает факты
через gh api и печатает диагноз тем же предикатом, которым пользуется
scheduler.py — не второе, расходящееся определение "что блокирует".

Использование: python3 scripts/gh/pr_blockers.py <номер PR>
"""
import importlib.util
import sys
from pathlib import Path

_net_spec = importlib.util.spec_from_file_location("net", Path(__file__).resolve().parent / "net.py")
net = importlib.util.module_from_spec(_net_spec)
_net_spec.loader.exec_module(net)

_rl_spec = importlib.util.spec_from_file_location(
    "review_labels", Path(__file__).resolve().parents[1] / "lib" / "review_labels.py")
review_labels = importlib.util.module_from_spec(_rl_spec)
_rl_spec.loader.exec_module(review_labels)


def main() -> None:
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        raise SystemExit("Использование: python3 scripts/gh/pr_blockers.py <номер PR>")
    pr = sys.argv[1]
    data = net.gh_api(f"repos/{{owner}}/{{repo}}/pulls/{pr}")
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
