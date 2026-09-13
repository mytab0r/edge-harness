#!/usr/bin/env bash
# Гвардия класса «не-list ответ страницы GitHub API трактуется как честная
# короткая страница» (дефект A, watchdog-issue #120, 2026-09-11): пять мест
# репозитория несли собственную копию `if not isinstance(chunk, list) or not
# chunk: break/return` — все сведены в review_labels.list_pages (fail loud на
# неожиданной форме ответа), этот файл держит класс закрытым механически.
set -euo pipefail
pip install --quiet pytest pyyaml
python -m pytest scripts/lib/test_silent_empty_page_guard.py -q
