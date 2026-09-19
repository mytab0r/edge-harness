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
#   1a) вернуть `-f` в ОДНУ строку curl внутри canary_http (строка с
#      `code=$(curl -sS -o "$body_file"` ДО определения canary_probe) —
#      «4 failed, 7 passed», краснеет четвёрка:
#      test_non_2xx_prints_code_and_body,
#      test_empty_error_body_is_named_as_empty_not_silently_skipped,
#      test_huge_body_is_truncated_and_says_so,
#      test_lowered_annotation_level_still_prints_the_body.
#      probe-тесты ЗЕЛЁНЫЕ: у canary_probe свой вызов curl, эта мутация его
#      не трогает.
#   1b) вернуть `-f` в ОБЕ строки curl (sed по всему файлу) — «5 failed,
#      6 passed»: к четвёрке добавляется
#      test_probe_prints_body_when_code_differs_from_expected, потому что
#      теперь тело отбирается и у пробы.
#      Два номера у одной строки рецепта — не разночтение, а две РАЗНЫЕ
#      мутации: «одна строка» и «весь файл» дают разные числа, и раньше
#      рецепт называл область словами «в canary_http», а число приводил от
#      второй. Обе исполнены на head 91eabef; область правки теперь названа
#      дословно, чтобы следующий читатель получил ровно эти числа.
#   2) снять вызов _canary_print_body из ветки не-2xx canary_http —
#      «4 failed, 7 passed», та же четвёрка, что у (1a): мутация глушит
#      печать только в canary_http, у probe своя ветка вызова
#      _canary_print_body.
#      Записанное заранее «3 failed, 8 passed» при исполнении не
#      подтвердилось — ровно случай правила «рецепт мутации — исполни, не
#      вспоминай» (AGENTS.md), и он повторился на этом же файле дважды.
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
