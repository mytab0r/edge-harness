#!/usr/bin/env bash
# Тесты зонда Anthropic-маршрута (#1520): проверяется ПРАВИЛО URL и то, что
# секрет не утекает в отчёт. Сеть не трогаем — неверен был пересказ правила,
# а не сетевой шов (#1502: таблица, неверная для трёх строк из четырёх,
# родилась именно из пересказа `messagesApiRoot`).
set -euo pipefail
python -m pytest scripts/measure/test_anthropic_route_probe.py -q
