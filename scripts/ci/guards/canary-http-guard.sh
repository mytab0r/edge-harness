#!/usr/bin/env bash
# Гвардия «канарейка деплоя печатает тело ответа» (issue #1371).
#
# Класс: шаг CI, знающий и код, и тело ответа, не имеет права печатать только
# код. Живой случай — прогон deploy-dsh-edge.yml 35387441612: в логе осталось
# «curl: (22) The requested URL returned error: 500» и больше ничего, хотя
# морда ответила осмысленным JSON с причиной. Виноват флаг `-f` (`--fail`),
# который и придуман, чтобы тело на HTTP-ошибке не отдавать.
#
# Гвардия ПОВЕДЕНЧЕСКАЯ: поднимает настоящий HTTP-сервер, гоняет настоящий
# curl через scripts/lib/canary_http.sh и читает настоящий вывод. Текстовая
# проверка исходника доказала бы орфографию, а не поведение (класс #891/#893).
#
# Доказано мутациями — ИСПОЛНЕНО, не пересказано.
#   1) вернуть `-f` в canary_http — «3 failed, 6 passed»;
#   2) снять вызов _canary_print_body из ветки не-2xx canary_http — те же
#      «3 failed, 6 passed» и та же тройка:
#      test_non_2xx_prints_code_and_body,
#      test_empty_error_body_is_named_as_empty_not_silently_skipped,
#      test_huge_body_is_truncated_and_says_so.
#      Совпадение названо честно: обе мутации убирают ОДНО наблюдаемое
#      свойство разными способами — первая не даёт телу дойти до файла,
#      вторая не печатает дошедшее.
#   3) дать canary_probe контракт canary_http (печатать тело всегда, а не
#      только при несовпадении кода) — «2 failed, 7 passed», краснеют
#      test_probe_is_silent_when_code_is_the_expected_one и
#      test_probe_treats_303_login_as_success_not_as_failure: именно они
#      держат то, ради чего проба отделена от canary_http.
# База до мутаций и после отката — «9 passed».
#
# Регистрируется каталогом (#749), не рукописным шагом repo-ci.yml —
# ci-guard-registration.sh замораживает список рукописных шагов, новый шаг
# здесь провалил бы её.
set -euo pipefail
pip install --quiet pytest
python -m pytest scripts/lib/test_canary_http_guard.py -q
