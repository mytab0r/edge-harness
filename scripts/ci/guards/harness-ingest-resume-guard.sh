#!/usr/bin/env bash
# Гвардия #1049 «Тёплый хэндл ingest» (PATCHES.md, «Тёплый хэндл ingest»):
# исполняет поведенческие тесты против planHarnessIngestResume/
# advanceHarnessIngestBaseTurn, извлечённых ДОСЛОВНО из текста патча
# 0004 (Node 24 type-stripping), и структурные проверки проводки
# appendHarnessEvents (план вызван, результат потреблён, entry положен в
# harnessIngestHandles, baseTurn двигается инкрементально, старый
# after-batch dispose исчез) — класс «патч пережил бамп апстрима и потерял
# проводку кэша» красит CI, а не проходит зелёным (ревью PR #1057,
# блокер 2).
#
# Зарегистрирована файлом каталога (#749), не рукописным шагом repo-ci.yml:
# класс «рукописный шаг вместо каталога» закрыт самой формой — этот файл и
# есть регистрация (гвардия ci_guard_registration_guard.py на рукописный
# шаг отвечала отказом, ратчет ALLOWLIST список не растит).
set -euo pipefail
node --test dsh-edge/test/harness-ingest-resume.test.mjs
