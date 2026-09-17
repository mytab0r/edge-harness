#!/usr/bin/env bash
# Гвардия класса #1286 (живой отказ — прогон worker.yml 34893177035): отказ
# на стороне провайдера в scripts/worker/task.sh не топит job воркера —
# задача возвращена в пул, сигнал живёт в комментарии/Telegram; НАШ отказ
# (prompt_too_long, #1315) умирает громко, кормя предохранитель диспатча.
# Носитель тела — scripts/worker/test/provider-exhaustion-exit-code.smoke.sh:
# исполняет НАСТОЯЩИЙ блок из task.sh на заглушках каналов; доказано
# мутациями (см. заголовок теста и тело PR #1313). Удалишь обёртку —
# канарейка осиротевших тестов красит.
set -euo pipefail
bash scripts/worker/test/provider-exhaustion-exit-code.smoke.sh
