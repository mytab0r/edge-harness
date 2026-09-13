#!/usr/bin/env bash
# Гвардия MAJOR-консистентности peer-зависимостей dsh-edge (#1041/#1087)
# доказана мутацией: зелёная на lockfile'е с допустимым major (в т.ч. с
# отставанием patch/minor — намеренно не находка), красная с точным
# сообщением на симуляции живого случая (react-dom@19.2.8 подставлен
# react@18.3.1).
set -euo pipefail
node --test dsh-edge/verify-peer-consistency.test.mjs
