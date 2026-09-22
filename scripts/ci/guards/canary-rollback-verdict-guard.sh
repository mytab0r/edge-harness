#!/usr/bin/env bash
# Гвардия класса «откат прода без вопроса о причине» (issue #1426).
#
# Прогон 35639422589 (deploy-worker.yml, job deploy на 61b185aa, слияние
# PR #1420): деплой прошёл, канарейка упала по таймауту локатора
# `#gate-token`, `wrangler rollback` откатил КОРРЕКТНЫЙ код. Поле не
# появилось не из-за деплоя — была исчерпана суточная квота rows_read
# Durable Objects (#1411): /api/status отдавал 500, страница не
# инициализировалась, статика отдавалась нормально. Кольцо: фикс квоты не
# задеплоить, пока квота исчерпана.
#
# Две стороны, обе обязательны:
#   * канарейка различает исходы (тест поведенческий: настоящий http.server,
#     настоящий node-скрипт — не заглушка);
#   * ни один шаг с `wrangler rollback` не запускается, не спросив вердикт.
#
# Доказано мутациями — ИСПОЛНЕНО, не пересказано.
# База: канарейка «4 passed», гвардия отката «10 passed».
#   1) убрать распознавание storage_quota_exceeded (считать любой 5xx виной
#      деплоя) — «1 failed, 3 passed», краснеет
#      test_quota_exhaustion_is_backend_down_and_forbids_rollback: живой
#      случай #1426 возвращается;
#   2) считать инфраструктурным ЛЮБОЙ 5xx (обратный перекос) — «2 failed,
#      2 passed», краснеют test_unrecognized_failure_still_counts_as_bad_deploy
#      и test_cloudflare_error_page_is_not_recognized_as_infrastructure:
#      сломанный деплой, положивший API, перестал бы откатываться — канарейка
#      лишилась бы главной работы;
#   3) убрать зонд из начала файла (состояние до #1426, где до проверки
#      /api/ready исполнение просто не доходило) — «4 failed»: краснеют ВСЕ
#      четыре. Заявлял «3 failed, 1 passed» по рассуждению — исполнение дало
#      другое, и здесь стоит исполненное. Причина разницы содержательна:
#      вместе с зондом исчезают и строки вердикта, по которым тесты отличают
#      «канарейка дошла до решения» от «умерла раньше», — то есть мутация
#      сносит не одну ветку, а весь признак;
#   4) снять условие вердикта с шага автооката deploy-worker.yml — «2 failed,
#      8 passed», краснеют test_live_repository_is_consistent и
#      test_live_repository_really_has_rollback_steps.
#
# Регистрируется каталогом (#749), не рукописным шагом repo-ci.yml.
set -euo pipefail
pip install --quiet pytest pyyaml
python scripts/lib/canary_rollback_guard.py
python -m pytest scripts/lib/test_canary_rollback_guard.py -q
python -m pytest scripts/lib/test_canary_rollback_verdict.py -q
