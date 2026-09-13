#!/usr/bin/env bash
# Перенесено из .github/workflows/repo-ci.yml транслятором рукописных
# шагов гвардий (issue #897, продолжение #749/#771/#762/#764): исходный
# шаг «Гвардия «сырой stderr клиента модели без redact» (#743)» перенесён сюда автоматически, при механическом
# ребейзе, без содержательной правки run: — исходный комментарий шага
# (если был) приведён ниже дословно.
#
# Утечка секрета (#743, живой случай — DSH_CHAIN_CLASS_NOTE,
# scripts/lib/dsh-ci.sh, до фикса): сырой stderr клиента модели уходил
# в публичный лог без redact() — GitHub маскирует только точное
# совпадение секрета, производное (эхо заголовка Authorization) не
# маскируется. Гвардия сканирует scripts/**/*.sh на команды-извлечения
# содержимого файла (cat/tr/cut/head/tail/sed/…), применённые к
# err_file/stderr.txt/answer_file/answer.txt, без redact в том же
# pipe-конвейере — новое такое место красит CI.
set -euo pipefail
pip install --quiet pytest pyyaml
python -m pytest scripts/lib/test_dsh_stderr_redact_guard.py -q
