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
from pathlib import Path

_net_spec = importlib.util.spec_from_file_location("net", Path(__file__).resolve().parent / "net.py")
net = importlib.util.module_from_spec(_net_spec)
_net_spec.loader.exec_module(net)

_rl_spec = importlib.util.spec_from_file_location(
    "review_labels", Path(__file__).resolve().parents[1] / "lib" / "review_labels.py")
review_labels = importlib.util.module_from_spec(_rl_spec)
_rl_spec.loader.exec_module(review_labels)


def main() -> None:
    pulls = review_labels.list_pages(
        "repos/{owner}/{repo}/pulls?state=open&per_page=100", net.gh_api)
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
