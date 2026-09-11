#!/usr/bin/env bash
# Гвардия класса «встроенный GITHUB_TOKEN не порождает проверок» (issue
# #218, следствие #929/#955): новый workflow с on.push по main, забытый в
# config/merge-reactions.json (ни как реакция, ни как осознанное
# исключение), красит CI — scripts/lib/merge_reactions_registry_guard.py.
set -euo pipefail
pip install --quiet pytest pyyaml
python scripts/lib/merge_reactions_registry_guard.py
python -m pytest scripts/lib/test_merge_reactions_registry_guard.py -q
