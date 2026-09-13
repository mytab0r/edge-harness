#!/usr/bin/env bash
# Перенесено из .github/workflows/repo-ci.yml в каталог гвардий (#749, #1069:
# долг ALLOWLIST — исходный шаг «Гвардия concurrency — не сериализовать
# разные джобы одной статической группой» несёт инлайн heredoc-python, у
# которого нет отдельного исполняемого файла — guard_step_translator.py не
# может определить имя файла каталога детерминированно («run: не содержит
# распознаваемого исполняемого файла»), перенесено вручную, тело run:
# скопировано дословно.
#
# Гвардия класса #189: concurrency-группа на уровне ФАЙЛА, если она не
# параметризована по PR/ref/task (статическая строка), сериализует ВСЕ
# джобы файла между собой. При двух и более джобах с разной семантикой
# (например обязательная PR-проверка + периодическая очередь) это
# вытесняет проверку чужими прогонами — все PR молча теряют статус
# (было в orchestra.yml: contract пропадал у каждого PR). Concurrency в
# таком файле обязана быть объявлена на уровне каждого джоба отдельно.
set -euo pipefail
pip install --quiet pyyaml
python - <<'PY'
import glob, sys, yaml
bad = []
for path in sorted(glob.glob('.github/workflows/*.yml')):
    doc = yaml.safe_load(open(path, encoding='utf-8'))
    conc = doc.get('concurrency')
    jobs = doc.get('jobs', {}) or {}
    if conc is None or len(jobs) < 2:
        continue
    group = str(conc.get('group', '')) if isinstance(conc, dict) else str(conc)
    expr_marker = '$' + '{{'
    is_static = expr_marker not in group
    if is_static:
        bad.append(
            f'{path}: concurrency.group="{group}" статична и объявлена на '
            f'уровне файла с {len(jobs)} джобами ({", ".join(jobs)}) — '
            'перенеси concurrency на уровень каждого джоба (класс #189)'
        )
if bad:
    sys.exit('\n'.join(bad))
print('concurrency: ни один многоджобовый workflow не сериализован статической файловой группой')
PY
