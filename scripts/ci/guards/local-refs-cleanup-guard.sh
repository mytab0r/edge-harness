#!/usr/bin/env bash
# Уборка мёртвых локальных git-refs разработчика (#940): ветки agent/*,
# кэш-ссылки refs/remotes/pr/<N>, огрызки refs/tmp/* от claim_task.py. Три
# поведенческих класса проверены на настоящем временном git-репозитории
# (tempfile + git init + реальные ветки/refs), не текст исходника —
# переименование/вырезание проверки безопасности красит поведение.
set -euo pipefail
pip install --quiet pytest
python -m pytest scripts/lib/test_local_refs_cleanup_guard.py -q
