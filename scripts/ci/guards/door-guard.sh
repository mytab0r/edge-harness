#!/usr/bin/env bash
# Канарейка «мимо двери нельзя» (#611): программное заведение issue пула
# (POST repos/{repo}/issues) обязано идти через scripts/lib/pool_issue.py::
# create_pool_issue, открытие PR — через scripts/git/pr-create. Единый
# реестр дверей scripts/lib/door_guard.py::DOORS покрывает CLI-форму (`gh
# issue create`/`gh pr create` вне обёртки) и REST-форму (сырой POST на
# коллекцию `.../issues`/`.../pulls`) во scripts/**, .github/workflows/**,
# cf-worker/**, dsh-edge/**, plugins-src/**. Легитимные сторонние двери
# (cf-worker/src/harness.ts, plugins-src/runner-bridge — рантаймы, не
# умеющие позвать bash/python-обёртку) размечены `door-exception:` рядом со
# строкой. Регистрация данными (каталог гвардий #749), не рукописный шаг
# .github/workflows/repo-ci.yml.
set -euo pipefail
pip install --quiet pytest
python -m pytest scripts/lib/test_door_guard.py -q
