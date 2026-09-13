#!/usr/bin/env bash
# #1163: appendHarnessEvents (dsh-edge/patches/0004-harness-ingest.patch)
# больше не диспоузит AgentHandle после ingest-батча — на dsh-edge 0.14.0
# openAgentForTurn — @deprecated-алиас резидентного getOrResumeAgent
# (docs/research/11-dsh-edge.md), и disposal без снятия из резидентного кэша
# оставлял мёртвый хэндл для СЛЕДУЮЩЕГО вызова той же сессии — живой
# прод-инцидент (run 34773180930, "session ... is not live in this store").
# Гвардия исполняет РЕАЛЬНЫЙ appendHarnessEvents, извлечённый дословно из
# текста патча (Node 24 type-stripping), против стаба резидентного кэша
# апстрима: dsh-edge/verify-ingest-resident-safety.mjs. Зарегистрирована
# файлом каталога (#749), не рукописным шагом repo-ci.yml.
set -euo pipefail
node --test dsh-edge/test/ingest-resident-safety.test.mjs
