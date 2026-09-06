#!/usr/bin/env python3
"""Замер для задачи #203: сколько событий labeled/unlabeled и прогонов contract
они порождают. Одноразовый диагностический скрипт (не гвардия), запускается
вручную: python3 scripts/measure/label_churn_203.py <ISO-окно-с> <ISO-окно-по>

Считает по всем открытым PR таймлайн-события labeled/unlabeled за окно и
раскладывает по имени метки. Каждое событие labeled = один триггер
`orchestra.yml` (on: pull_request: [labeled]) = один прогон job'а contract.
"""

import json
import os
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone


def gh(url: str):
    result = subprocess.run(["gh", "api", url], capture_output=True, text=True,
                            env={**os.environ, "NO_COLOR": "1"})
    if result.returncode != 0:
        raise RuntimeError(f"gh api {url}: {result.stderr.strip()}")
    return json.loads(result.stdout)


def paged(url: str):
    page = 1
    items = []
    while True:
        chunk = gh(f"{url}{'&' if '?' in url else '?'}per_page=100&page={page}")
        if not isinstance(chunk, list) or not chunk:
            break
        items.extend(chunk)
        if len(chunk) < 100:
            break
        page += 1
    return items


def main() -> int:
    since = datetime.fromisoformat(sys.argv[1]).replace(tzinfo=timezone.utc)
    until = datetime.fromisoformat(sys.argv[2]).replace(tzinfo=timezone.utc)
    repo = os.environ["GITHUB_REPOSITORY"]
    pulls = paged(f"repos/{repo}/pulls?state=open")
    print(f"открытых PR: {len(pulls)}", file=sys.stderr)
    labeled = Counter()
    unlabeled = Counter()
    for pull in pulls:
        for event in paged(f"repos/{repo}/issues/{pull['number']}/timeline"):
            if event.get("event") not in ("labeled", "unlabeled"):
                continue
            created = datetime.fromisoformat(event["created_at"].replace("Z", "+00:00"))
            if not (since <= created < until):
                continue
            name = event.get("label", {}).get("name", "?")
            (labeled if event["event"] == "labeled" else unlabeled)[name] += 1
    total_l = sum(labeled.values())
    total_u = sum(unlabeled.values())
    print(f"окно {since.isoformat()} .. {until.isoformat()}")
    print(f"событий labeled: {total_l}  (= прогонов contract от labeled)")
    print(f"событий unlabeled: {total_u}")
    for name, count in labeled.most_common():
        print(f"  labeled {name}: {count}")
    for name, count in unlabeled.most_common():
        print(f"  unlabeled {name}: {count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
