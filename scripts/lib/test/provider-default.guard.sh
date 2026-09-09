#!/usr/bin/env bash
# Гвардия класса #153: провайдер/модель LLM — ровно одно место правды
# (vars.DEEPSEEK_BASE_URL/DEEPSEEK_MODEL репозитория). Вынесена в отдельный
# скрипт (ревью #153, находка 9): repo-ci.yml раньше исключал себя из
# сканирования ЦЕЛИКОМ, из-за чего сама гвардия была слепа к себе; здесь
# исключается только этот файл. Сканирует БЕЗ --include-фильтров по
# расширению — раньше `*.yml`/`*.sh` не покрывали scripts/git/task-branch
# (без расширения) и scripts/**/*.py — гвардия слепа к части файлов, второе
# определение «что считается скриптом» (repo-ci.yml:140 включает
# scripts/git/* как bash) не совпадало с allowlist здесь.
#
# Два паттерна ловят разные классы регресса (доказано мутацией по отдельности):
#
#   (а) СВОЙСТВО — конструкция фолбэка рядом с нашими переменными:
#       `${DEEPSEEK_MODEL:-...}`, `vars.DEEPSEEK_BASE_URL || '...'` и т.п.
#       Ловит НОВЫЙ фолбэк независимо от того, какой конкретно провайдер/
#       модель зашиты (другой провайдер, ЗАГЛАВНЫЕ, URL по частям,
#       $FALLBACK_MODEL, vars.LEGACY_MODEL — свойство синтаксиса одно и то же).
#
#   (б) ЛИТЕРАЛЬНЫЙ — конкретные строки прежних дефолтов (api.z.ai,
#       integrate.api.nvidia.com, glm-N, nemotron, deepseek-vN). Ловит
#       СТЕЙЛ-значения в прозе (документация, комментарии), которые
#       property-паттерн не видит, потому что там нет `||`/`:-` рядом с
#       DEEPSEEK_*. Применяется к строкам, НЕ являющимся комментарием
#       (первый непробельный символ — не `#`) — доказательные комментарии
#       («без патча уходит deepseek-v4-flash», «потолок GLM 131072») несут
#       факт живого прогона и не обязаны быть выхолощены ради зелёного grep.
#       Исключение: docs/agents/** — файл идёт КОНКАТЕНАЦИЕЙ прямо в промпт
#       агента (scripts/worker/task.sh читает WORKER-PLAYBOOK.md целиком),
#       там литералов не должно быть вовсе — ни в прозе, ни в комментариях
#       (в .md комментариев как таких нет, но правило применяется к каждой
#       строке файла без исключений).
#
# Allowlist (после сужения находкой 7 в deploy-dsh-edge.yml их стало меньше):
#   - scripts/lib/test/dsh-clients.smoke.sh — фикстура смоука (непустая
#     строка для dsh_require_provider_env, не источник правды);
#   - .github/workflows/deploy-dsh-edge.yml — patch каталога моделей МОРДЫ:
#     "deepseek-v4-flash" в regex-паттерне — upstream-маркер бандла dsh-edge
#     для замены, не наш CI-дефолт вызова LLM. Исключается ТОЧЕЧНО (по этой
#     строке), а не файл целиком (находка 4).
#   - scripts/lib/dsh-ci.sh, массив PLUGINS_SUITE_CANDIDATE_ROUTES (#215): не
#     фолбэк-дефолт класса #153 (нет `${DEEPSEEK_*:-...}`, единственный
#     обязательный провайдер по-прежнему только vars.DEEPSEEK_BASE_URL/MODEL)
#     — явная, опционально подключаемая таблица маршрутов combo-router;
#     значения взяты буквально из собственного примера suite
#     (dsh-combo-router/examples/mytab0r.settings.yml). Маршрут активируется,
#     только если задан секрет соответствующей учётки — молчаливый дефолт
#     здесь невозможен по построению самого combo-router (пустой routes при
#     enabled:true падает конструктором роутера).
#   - scripts/lib/test_provider_secrets_import.py, фикстура DSH_CI_FIXTURE
#     (#733): синтетический клон формата PLUGINS_SUITE_CANDIDATE_ROUTES с
#     заменёнными на "model-a/b/c" именами моделей — но реальные URL
#     (integrate.api.nvidia.com, api.z.ai) в фикстуре остаются буквальными,
#     потому что тест разбирает реальный формат строки, не придуманный;
#     это не фолбэк-дефолт, а тестовые данные.
#   - scripts/measure/provider_latency.py, таблица PROVIDER_LATENCY_CANDIDATES
#     (#836): тот же приём, что PLUGINS_SUITE_CANDIDATE_ROUTES выше — явная
#     таблица провайдеров-кандидатов для ЖИВОГО замера латентности, не
#     фолбэк-дефолт класса #153 (нет `${DEEPSEEK_*:-...}`, единственный
#     обязательный провайдер по-прежнему только vars.DEEPSEEK_BASE_URL/MODEL).
#     Литералы взяты буквально из постановки задачи #836; каждая строка
#     списка отличима именем провайдера ("NVIDIA-nano" и т.д.).
#   - scripts/measure/test_provider_latency.py — файл целиком (тот же приём,
#     что у смоук-фикстуры dsh-clients.smoke.sh выше): существует ради
#     разбора прод-формы URL/модели этих же провайдеров, не источник правды.
#   - этот файл (regex-литералы самой гвардии).
set -euo pipefail

SCOPE=".github/workflows scripts docs/agents"
SELF="scripts/lib/test/provider-default.guard.sh"

files() {
  # Только текстовые файлы, без __pycache__/бинарного мусора; сам файл
  # гвардии исключён поимённо (не директорией).
  find $SCOPE -type f \
    ! -path '*/__pycache__/*' \
    ! -path "./$SELF" \
    ! -name '*.pyc' \
    ! -name '*.tgz' ! -name '*.png' ! -name '*.jpg' ! -name '*.svg' ! -name '*.ico' \
    | while read -r f; do
        case "$f" in "$SELF") continue ;; esac
        if grep -Iq . "$f" 2>/dev/null; then printf '%s\n' "$f"; fi
      done
}

fail=0

# ── (а) property-паттерн: фолбэк рядом с нашими переменными ──────────────────
property_hits=$(files | xargs grep -nE 'DEEPSEEK_(BASE_URL|MODEL|API_KEY)([^}]*\|\||:-[^}"'"'"'])' 2>/dev/null || true)
if [ -n "$property_hits" ]; then
  echo "::error::Зашитый фолбэк рядом с DEEPSEEK_* (property-паттерн, класс #153) — единственное место правды vars.DEEPSEEK_BASE_URL/DEEPSEEK_MODEL нарушено"
  echo "$property_hits"
  fail=1
fi

# ── (б) литеральный паттерн: стейл-значения прежних дефолтов ────────────────
LITERAL_RE='api\.z\.ai|integrate\.api\.nvidia\.com|glm-[0-9]|nemotron|deepseek-v[0-9]'
literal_hits=""
while IFS= read -r f; do
  is_docs_agents=0
  case "$f" in docs/agents/*) is_docs_agents=1 ;; esac
  hit=$(grep -nE "$LITERAL_RE" "$f" 2>/dev/null || true)
  [ -z "$hit" ] && continue
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    lineno="${line%%:*}"
    content=$(sed -n "${lineno}p" "$f")
    trimmed="${content#"${content%%[![:space:]]*}"}"
    is_comment=0
    case "${trimmed:0:1}" in '#') is_comment=1 ;; esac
    if [ "$is_docs_agents" = 1 ] || [ "$is_comment" = 0 ]; then
      # allowlist точечных легитимных не-комментарийных литералов
      case "$f:$lineno" in
        "scripts/lib/test/dsh-clients.smoke.sh:"*) continue ;;
        # Тестовые данные бенчмарка латентности (#836) — тот же приём, что
        # у смоук-фикстуры выше: файл целиком существует ради разбора
        # прод-формы URL/модели этих же провайдеров, не источник правды.
        "scripts/measure/test_provider_latency.py:"*) continue ;;
      esac
      if [ "$f" = "scripts/lib/dsh-ci.sh" ]; then
        case "$content" in *PLUGINS_SUITE_CANDIDATE_ROUTES*|*'"nvidia-nim-'*|*'"zai-'*|*'"ollama-cloud-'*|*'"openrouter-'*) continue ;; esac
      fi
      if [ "$f" = "scripts/lib/test_provider_secrets_import.py" ]; then
        case "$content" in *'"nvidia-nim-'*|*'"zai-'*) continue ;; esac
      fi
      if [ "$f" = ".github/workflows/deploy-dsh-edge.yml" ]; then
        case "$content" in *'deepseek-v4-flash"'*) continue ;; esac
      fi
      if [ "$f" = "scripts/measure/provider_latency.py" ]; then
        case "$content" in *'"NVIDIA-nano"'*|*'"NVIDIA-ultra"'*|*'"Ollama"'*|*'"OpenRouter"'*|*'"GLM"'*) continue ;; esac
      fi
      literal_hits="$literal_hits$f:$line
"
    fi
  done <<<"$hit"
done < <(files)

if [ -n "$literal_hits" ]; then
  echo "::error::Стейл-литерал прежнего дефолта провайдера/модели (литеральный паттерн, класс #153) вне allowlist"
  printf '%s' "$literal_hits"
  fail=1
fi

if [ "$fail" = 0 ]; then
  echo "провайдер/модель: ни property-, ни литеральных зашитых дефолтов вне allowlist нет"
fi
exit "$fail"
