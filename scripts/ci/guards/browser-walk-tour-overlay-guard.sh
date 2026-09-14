#!/usr/bin/env bash
# Юнит-тест closeTourOverlay (флейк #1066, run 34700537627): смоук
# dsh-edge-pr-smoke флейкал на locator.click: Timeout по кнопке «Settings»,
# перехваченной маской оверлей-тура первого визита — старый код глушил
# отказ клика по «Continue» через `.catch(() => {})` и ждал фиксированную
# паузу вместо факта закрытия. Гвардия — дословный доказанный мутацией тест
# (см. докстринг dsh-edge/e2e-smoke/browser-walk.test.mjs): откат фикса на
# старую форму красит эти тесты. Зарегистрирована файлом каталога (#749),
# без правки repo-ci.yml. Зависимостей playwright-core не требует —
# closeTourOverlay принимает duck-typed Locator, реального браузера тест не
# поднимает.
set -euo pipefail
node --test dsh-edge/e2e-smoke/browser-walk.test.mjs
