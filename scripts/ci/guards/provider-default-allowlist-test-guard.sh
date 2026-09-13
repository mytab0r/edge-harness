#!/usr/bin/env bash
# Регрессионный тест путевого правила #1111 (allowlist гвардии
# provider-default.guard.sh — литеральный паттерн пропускает тестовые
# фикстуры по пути, property-паттерн ловит их наравне со всеми). Прогоняет
# РЕАЛЬНЫЙ файл гвардии на синтетическом дереве —
# scripts/lib/test/provider-default-guard-allowlist.test.sh, не пересказ.
set -euo pipefail
bash scripts/lib/test/provider-default-guard-allowlist.test.sh
