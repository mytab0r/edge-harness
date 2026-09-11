#!/usr/bin/env bash
# Гвардия класса «значение метки в query GitHub API не URL-кодировано» (#938).
#
# Инцидент: waiting_owner_guard.py::open_waiting_owner_issues строил
# `labels={WAITING_OWNER_LABEL}` (WAITING_OWNER_LABEL = "waiting:owner") без
# кодирования — GitHub молча отвечал пустым списком вместо ошибки, детектор
# ответа владельца не видел ни одной задачи двое суток (ответ «РЕШЕНИЕ: 1»
# в #782 повис необработанным). Одно место правды на кодирование —
# review_labels.label_query_value; гвардия по исходнику требует, чтобы
# КАЖДАЯ подстановка переменной после `labels=` в query шла через неё, плюс
# поведенческий тест на прод-форме (`waiting:owner` -> `waiting%3Aowner`).
set -euo pipefail
python -m pytest scripts/lib/test_label_query_encoding_guard.py -q
