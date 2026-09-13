#!/usr/bin/env bash
# Носитель тела гвардии «workflow из docs существует» (#1069, ревью PR #1117,
# находка 2 — тот же механизм, что workflow-concurrency.guard.sh): инлайн
# heredoc-python не оставлял файла-носителя, удаление обёртки было бы
# молчаливым. Тело перенесено дословно.
#
# Гвардия класса #72: docs обещают ворота, которых нет. ADR 0004 обещал job
# `canary` в `worker-ci`, workflow снесён cleanup-коммитом 09ef969 — ADR молча
# продолжал обещать проверки на PR, и класс инцидента ADR (сломанная страница
# при зелёном CI) снова доезжал до прода. Ссылки на .github/workflows/*.yml
# в docs/ обязаны указывать на существующий файл. docs/research/ исключён:
# там чужие системы, их CI живёт в их репозиториях; openspec/changes/ —
# планируемое будущее, а не установленный факт.
set -euo pipefail
python - <<'PY'
import re, sys
from pathlib import Path

pattern = re.compile(r"\.github/workflows/([A-Za-z0-9._/-]+\.ya?ml)")
missing, total = [], 0
for path in sorted(Path("docs").rglob("*.md")):
    if path.as_posix().startswith("docs/research/"):
        continue
    for match in pattern.finditer(path.read_text(encoding="utf-8")):
        total += 1
        ref = match.group(0)
        if not Path(".github/workflows", match.group(1)).is_file():
            missing.append(f"{path.as_posix()}: ссылка на несуществующий workflow {ref}")
if missing:
    sys.exit("\n".join(missing))
print(f"docs: все {total} ссылок на .github/workflows/ существуют")
PY
