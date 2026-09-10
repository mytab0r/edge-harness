#!/usr/bin/env bash
# Юнит-тесты класса «тихий ноль» в двух канарейках dsh-edge (#635):
# verify-ingest-allowlist.mjs и smoke-edge-plugins.mjs — расхождение формата
# извлечения (regex разошёлся с реальной формой источника) обязано падать
# громко, а не читаться как «кандидатов нет» с зелёным exit 0. Тесты кормятся
# прод-формой (патч 0004, scripts/dsh-hands-streamer/lib/core.js,
# кодогенератор edge-plugins.generated.ts) и покрывают совместный дрейф обоих
# regex'ов извлечения. Зарегистрирована файлом каталога (#749), не рукописным
# шагом repo-ci.yml; setup-node делает сам job `test` выше шага-перебора.
set -euo pipefail
node --test dsh-edge/test/verify-ingest-allowlist.test.mjs
node --test dsh-edge/test/smoke-edge-plugins.test.mjs
