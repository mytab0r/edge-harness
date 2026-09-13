#!/usr/bin/env bash
# Одно место правды на append+push с ретраем на data-ветку (#882): раньше
# эта логика была независимой копией в pipeline_health.py/dispatch_tail.py
# с одним и тем же дефектом (тихий check=False маскировал отказ
# авторизации push под безобидную гонку параллельного писателя — живой
# инцидент, снимки здоровья конвейера не записывались НИ РАЗУ). Реальный
# git, локальный bare-репозиторий: гонка, отказ авторизации (pre-receive
# хук с текстом, похожим на GitHub 403), первое создание ветки.
set -euo pipefail
pip install --quiet pytest
python -m pytest scripts/lib/test_data_branch_writer.py -q
