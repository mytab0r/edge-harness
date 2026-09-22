#!/usr/bin/env bash
# Гвардия класса «деплойный workflow не под наблюдением» (issue #1421).
#
# Инвариант 17 (repo_invariants.check_frontend_deploy_stale) умеет назвать
# факт «прод молча устарел» — но только для workflow из рукописного списка
# FRONTEND_DEPLOY_WORKFLOWS. #1041 написал его под один файл; второй,
# deploy-worker.yml, краснел девять суток (2026-09-13 … 2026-09-21), прод-морда
# не обновлялась, и заметил это человек, открывший вкладку Actions руками
# (#1419). #1419 дописал второй файл в список — починил СЛУЧАЙ. Класс в том,
# что список ведётся руками: третий деплойный workflow станет невидим тем же
# способом.
#
# Здесь признак «этот workflow деплоит» читается из самих файлов
# .github/workflows (шаг с wrangler), а списки инварианта сверяются с ним в обе
# стороны: незарегистрированный деплой, мёртвая запись, запись без шага
# wrangler, противоречие «наблюдаем + не наблюдаем».
#
# Доказано мутациями — ИСПОЛНЕНО, не пересказано. База: «13 passed».
#   1) вернуть список к одному workflow (убрать deploy-worker.yml из
#      FRONTEND_DEPLOY_WORKFLOWS, то есть откатить репозиторий в состояние до
#      #1419) — «2 failed, 11 passed»: краснеют
#      test_live_repository_is_consistent и
#      test_live_repository_really_has_deploy_workflows. Это и есть замер:
#      состояние, простоявшее девять суток незамеченным, теперь красит CI
#      немедленно;
#   2) выбросить `versions upload` из признака деплоя — «1 failed, 12 passed»,
#      краснеет test_versions_upload_counts_as_deploy: дверь в тот же прод,
#      которую гвардия перестала узнавать;
#   3) перестать вырезать комментарии перед сопоставлением — «1 failed,
#      12 passed», краснеет test_deploy_only_in_a_comment_is_not_a_deploy.
#      На ЖИВОМ дереве та же мутация не меняет ничего (множество деплойных
#      совпадает) — сказано прямо в докстринге _strip_shell_comments: это
#      защита вперёд, не починка живого случая;
#   4) снять проверку «запись есть, шага wrangler нет» — «1 failed,
#      12 passed», краснеет test_listed_workflow_that_stopped_deploying_reddens:
#      без неё уехавший признак давал бы ложно-зелёную гвардию, а это хуже
#      отсутствующей (#891/#893).
#
# Регистрируется каталогом (#749), не рукописным шагом repo-ci.yml.
set -euo pipefail
pip install --quiet pytest pyyaml
python scripts/lib/deploy_workflow_registry_guard.py
python -m pytest scripts/lib/test_deploy_workflow_registry_guard.py -q
