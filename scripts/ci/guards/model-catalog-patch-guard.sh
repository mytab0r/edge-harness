#!/usr/bin/env bash
# Гвардия шага «Патч каталога моделей под реального провайдера»
# (.github/workflows/deploy-dsh-edge.yml, задача #1294). Класс отказа живой:
# якорь патча был привязан к ПОРЯДКУ элементов чужого DEFAULT_MODELS, апстрим
# 0.1.5-rc.2 поставил первым новую модель — шаг упал, деплой встал, морда
# осталась на 0.14.1 при пине 0.15.0. Тест кормится прод-формой: дословный
# массив апстрима после настоящего esbuild --minify (провенанс — в докстринге
# теста), а регекс и маркер читаются ИЗ САМОГО workflow, не копией.
#
# Доказано мутациями — ИСПОЛНЕНО, не пересказано:
#   1) вернуть прежний якорь `[{id:"deepseek-v4-flash"` — «2 failed, 3 passed»,
#      краснеют test_catalog_pattern_matches_whole_array_on_live_upstream_form
#      и test_pattern_does_not_swallow_the_rest_of_the_bundle;
#   2) снять гейт уникальности маркера — «2 failed, 3 passed», краснеют
#      test_marker_is_unique_in_live_upstream_form и
#      test_step_refuses_loudly_when_marker_is_not_unique.
# База до мутаций и после отката — «5 passed».
#
# Регистрируется каталогом (#749), не рукописным шагом repo-ci.yml —
# ci-guard-registration.sh замораживает список рукописных шагов, новый шаг
# здесь провалил бы её.
set -euo pipefail
pip install --quiet pytest
python -m pytest scripts/lib/test_model_catalog_patch_guard.py -q
