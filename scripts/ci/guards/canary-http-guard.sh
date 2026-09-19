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
#   1) вернуть `-f` в canary_http — «5 failed, 6 passed», краснеет пятёрка:
#      test_non_2xx_prints_code_and_body,
#      test_empty_error_body_is_named_as_empty_not_silently_skipped,
#      test_huge_body_is_truncated_and_says_so,
#      test_probe_prints_body_when_code_differs_from_expected,
#      test_lowered_annotation_level_still_prints_the_body;
#   2) снять вызов _canary_print_body из ветки не-2xx canary_http —
#      «4 failed, 7 passed», краснеет та же четвёрка, КРОМЕ probe-теста:
#      test_non_2xx_prints_code_and_body,
#      test_empty_error_body_is_named_as_empty_not_silently_skipped,
#      test_huge_body_is_truncated_and_says_so,
#      test_lowered_annotation_level_still_prints_the_body.
#      Числа мутаций НЕ совпадают, и разница названа по именам: под `-f` curl
#      не отдаёт тело НИ ОДНОЙ ветке, поэтому у probe-теста несовпадения
#      (не-200 вместо ожидаемого кода) тело отбирается тоже — он краснеет
#      вместе с остальными; мутация (2) глушит печать только в canary_http,
#      у probe своя ветка вызова _canary_print_body, и probe-тесты остаются
#      зелёными. Записанное заранее «4 failed, 7 passed» для (1) при
#      исполнении НЕ подтвердилось — пять, не четыре: ровно случай правила
#      «рецепт мутации — исполни, не вспоминай» (AGENTS.md), пойманный
#      повторным исполнением при доводке PR #1372 (обе мутации исполнены
#      второй раз на head 84a4039, фактические числа здесь).
#   3) дать canary_probe контракт canary_http (печатать тело всегда, а не
#      только при несовпадении кода) — «2 failed, 9 passed», краснеют
#      test_probe_is_silent_when_code_is_the_expected_one и
#      test_probe_treats_303_login_as_success_not_as_failure: именно они
#      держат то, ради чего проба отделена от canary_http.
#   4) сменить умолчание уровня аннотации на warning
#      (`CANARY_ERROR_LEVEL:-error` → `:-warning`) — «1 failed, 10 passed»,
#      краснеет test_default_annotation_level_is_error: без неё опечатка в
#      умолчании сделала бы тихими ВСЕ канарейки разом.
#   5) не печатать тело на пониженном уровне (ранний return из
#      _canary_print_body при CANARY_ERROR_LEVEL != error) — «1 failed,
#      10 passed», краснеет test_lowered_annotation_level_still_prints_the_
#      body: класс #1371 вернулся бы через чёрный ход в единственном шаге,
#      которому разрешено быть нефатальным.
# База до мутаций и после отката — «11 passed».
#
# Регистрируется каталогом (#749), не рукописным шагом repo-ci.yml —
# ci-guard-registration.sh замораживает список рукописных шагов, новый шаг
# здесь провалил бы её.
set -euo pipefail
pip install --quiet pytest
python -m pytest scripts/lib/test_canary_http_guard.py -q
