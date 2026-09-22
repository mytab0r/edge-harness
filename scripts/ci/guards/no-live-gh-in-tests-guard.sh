#!/usr/bin/env bash
# Гвардия класса «тест уходит в живой GitHub» (issue #1438).
#
# Замер 2026-09-22 на полном прогоне scripts/orchestra (подмена
# subprocess.run, счёт вызовов gh): 19 тестов реально ходили в сеть. Из них
# один делал 52 запроса к НАСТОЯЩЕМУ repos/mytab0r/edge-harness (из того же
# бюджета установки, чьё исчерпание красит живые PR чужой причиной, #1437),
# другой — изменяющий вызов `gh api -X DELETE .../git/refs/locks/task-320`,
# а третий, `test_main_makes_zero_mutating_calls_on_fully_empty_queue`, сам
# сделал 21 живой вызов — гвардия холостого хода утекала мимо механизма,
# который сторожит.
#
# Механизм утечки — разные ЭКЗЕМПЛЯРЫ модуля в одном прогоне: тест патчит gh
# у своего scheduler, а upstream_drift внутри себя зовёт gh у чужого
# (замер: sys.modules['scheduler'] id=140137492084464, scheduler у
# upstream_drift id=9604480, «тот же gh?» False). CI этого не видит, потому
# что гоняет каждый файл отдельной командой, а в одиночном прогоне второго
# экземпляра не возникает.
#
# Носитель — scripts/orchestra/conftest.py: живой gh из теста бросает
# AssertionError с названием вызова. Нынешний долг перечислен в
# ALLOWED_LIVE_GH с причиной и может только сокращаться.
#
# Доказано мутациями — ИСПОЛНЕНО.
#   1) снять одну запись из ALLOWED_LIVE_GH
#      (test_after_merge_telegram_miss_is_loud_but_not_fatal), база
#      «430 passed» на test_scheduler.py — «1 failed», с текстом «тест ушёл
#      в ЖИВОЙ GitHub: gh api repos/o/r/actions/workflows/worker.yml/
#      runs?per_page=10». Возврат записи — снова зелено;
#   2) вырезать саму фикстуру forbid_live_gh, база «3 passed» на
#      test_no_live_gh_guard.py — краснеет
#      test_live_gh_from_a_test_is_refused_loudly: без этого файла удаление
#      фикстуры проходило бы молча, autouse-гвардия невидима в зелёном
#      наборе.
#
# Отдельно записан ОТРИЦАТЕЛЬНЫЙ результат, чтобы следующий не потратил на
# него заход: замена рукописного списка модулей в patch_gh на обнаружение
# (патчить gh у каждого загруженного модуля репозитория) даёт «277 failed,
# 153 passed» — список несущий, утечки чинятся поштучно.
#
# Регистрируется каталогом (#749), не рукописным шагом repo-ci.yml.
set -euo pipefail
pip install --quiet pytest
python -m pytest scripts/orchestra/test_no_live_gh_guard.py -q
