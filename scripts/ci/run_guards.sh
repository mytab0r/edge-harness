#!/usr/bin/env bash
# Перебор каталога гвардий (#749): регистрация гвардии в CI обязана быть
# данными, а не рукописным шагом .github/workflows/repo-ci.yml — иначе
# любые два PR, добавляющие гвардию, конфликтуют по построению (замер
# issue #749: 17 из 40 открытых PR правили один файл ради этого).
#
# Каждый файл scripts/ci/guards/*.sh — одна зарегистрированная гвардия,
# исполняется ЦЕЛИКОМ (bash "$script"): несёт свои pip install/pytest/
# node --test команды сам, ровно то же самое, что раньше делал рукописный
# шаг workflow, просто в отдельном файле. Добавление новой гвардии = новый
# файл здесь, без единой правки repo-ci.yml (критерий приёмки #749).
#
# `if:`/`env:`, которые сегодня несут некоторые рукописные шаги repo-ci.yml,
# несутся так: GH_TOKEN — `env:` шага-перебора «Каталог гвардий … — перебор»
# в repo-ci.yml (не job-уровня — ревью PR #771, minor 6: job-level `env:`
# расширял бы радиус до всех шагов job); дочерний `bash "$script"` этого шага
# наследует переменную без per-файловой проводки.
#
# ИСПРАВЛЕНО #1004: прежняя редакция обосновывала step-уровень фразой «ни одна
# гвардия каталога сегодня не читает gh api … без единого потребителя». Это
# перестало быть правдой, и надолго: замер 2026-09-22 нашёл ЧЕТЫРЕХ
# потребителей — ci-guard-registration, decision-doc-numbering-guard,
# mutation-claim-guard, plugin-manager-roster-guard. Обоснование step-уровня
# остаётся верным (радиус), но опирается теперь на радиус, а не на
# отсутствие потребителей. Каждый потребитель обязан пропускать свой `main`
# через `rate_guard.run_guard_main` — иначе исчерпанный бюджет красит
# обязательную проверку чужой причиной; держится проверкой
# scripts/lib/api_quota_gate_guard.py::catalogue_problems. Условие
# `github.event_name == 'push'` — сам файл гвардии читает $GITHUB_EVENT_NAME
# (штатная переменная окружения раннера GitHub Actions, доступна без
# дополнительной проводки). Единственный НЕ перенесённый случай — шаг,
# чьё `if:` зависит от OUTPUT другого шага этого же job (`steps.quota.
# outputs.skip`, гейт квоты перед инвариантами репозитория): такая
# зависимость специфична для одной пары шагов и не обобщается на каталог
# без спекулятивной инфраструктуры (design.md #749 называет это явно, не
# молчит) — соответствующий шаг остаётся рукописным в repo-ci.yml.
# Fail-fast — объявленное решение, не умолчание: первая упавшая гвардия
# останавливает прогон немедленно (`set -e` + явный `exit 1` ниже), CI видит
# ОДНУ причину за проход, не пачку вперемешку с гвардиями, которые могли бы
# упасть по цепочке от первой же (#749, ревью PR #771, минорная находка 12).
#
# GUARD_CATALOG_SKIP (issue #1280) — необязательный список имён гвардий
# (basename без .sh, через пробел), которые перебор регистрирует, но НЕ
# исполняет в шаге ниже. По умолчанию пуст — вызов `bash scripts/ci/
# run_guards.sh` без переменной (ровно то, что делает repo-ci.yml) ведёт
# себя как прежде, полный каталог, без единого исключения. Переменная нужна
# только локальному/dev-гейту `scripts/lib/pre_push_guard_gate.py`: два
# файла каталога (`decision-doc-numbering-guard.sh`, `invariant-numbering-
# guard.sh`) через общий примитив `decision_numbering.py::fetch_refs` делают
# `git fetch --depth 1` (ловушка #1228) — измерено 2026-09-15: единичный
# локальный прогон этого файла перевёл `git rev-parse --is-shallow-
# repository` false -> true, т.е. замусорил ОБЩИЙ .git всех рабочих деревьев
# задачи. В одноразовом чекауте CI (job `test`, воркер) той же мутации не за
# что зацепиться — общего .git с другими деревьями там нет, поэтому CI
# по-прежнему гоняет обе гвардии без исключений (переменную не выставляет).
guard_catalog_skip="${GUARD_CATALOG_SKIP:-}"
should_skip() {
  local name="$1" entry
  for entry in $guard_catalog_skip; do
    [ "$entry" = "$name" ] && return 0
  done
  return 1
}

set -euo pipefail
shopt -s nullglob

guards_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$guards_root/../.." && pwd)"
dir="$guards_root/guards"

if [ ! -d "$dir" ]; then
  echo "::error::scripts/ci/guards не существует — перебор гвардий невозможен"
  exit 1
fi

scripts=("$dir"/*.sh)

# Любая запись каталога верхнего уровня, не подошедшая под шаблон `*.sh`
# (другое расширение/регистр, файл без расширения, поддиректория), —
# молчаливая потеря гвардии по построению `nullglob`: файл физически лежит
# в scripts/ci/guards/, а перебор его не видит и не сообщает об этом.
# Живые прогоны гейта: `important-guard.bash`/файл без расширения/`.SH`/`.py`
# и вложенная `guards/orchestra/nested-guard.sh` — все давали `выполнено 1`
# вместо ожидаемых гвардий и EXIT=0 (#749, ревью PR #771, блокирующая 3).
unexpected=()
for entry in "$dir"/*; do
  match=0
  for script in "${scripts[@]}"; do
    if [ "$entry" = "$script" ]; then
      match=1
      break
    fi
  done
  if [ "$match" -eq 0 ]; then
    unexpected+=("$(basename "$entry")")
  fi
done
if [ "${#unexpected[@]}" -gt 0 ]; then
  echo "::error::scripts/ci/guards содержит запись(и) вне соглашения '*.sh' верхнего уровня, поэтому НЕ зарегистрированную как гвардия: ${unexpected[*]} — переименуй в *.sh каталога верхнего уровня или удали"
  exit 1
fi

if [ "${#scripts[@]}" -eq 0 ]; then
  echo "::error::scripts/ci/guards пуст — перебор не нашёл ни одного файла (пустой каталог красит CI, а не молча проходит нулём проверок)"
  exit 1
fi

# Файл каталога без единой команды (0 байт, либо только шебанг/`set -euo
# pipefail`) исполняется без ошибки (`bash` пустого файла — это exit 0) и
# молча ничего не проверяет: та же потеря, что и «файл вне соглашения»
# выше, только для файла, который сам под соглашение подходит.
for script in "${scripts[@]}"; do
  name="$(basename "$script" .sh)"
  meaningful="$(grep -vE '^[[:space:]]*(#.*)?$' "$script" | grep -vE '^[[:space:]]*set[[:space:]]+-' || true)"
  if [ -z "$meaningful" ]; then
    echo "::error::scripts/ci/guards/$name.sh не несёт ни одной команды (пустой файл или только шебанг/set) — гвардия не зарегистрирована"
    exit 1
  fi
done

count=0
skipped=0
for script in "${scripts[@]}"; do
  name="$(basename "$script" .sh)"
  if should_skip "$name"; then
    echo "guard-catalog: '$name' пропущена (GUARD_CATALOG_SKIP) — не полный каталог, см. комментарий выше"
    skipped=$((skipped + 1))
    continue
  fi
  echo "::group::guard: $name"
  count=$((count + 1))
  if ! (cd "$repo_root" && bash "$script"); then
    echo "::endgroup::"
    echo "::error::гвардия каталога '$name' (scripts/ci/guards/$name.sh) провалилась"
    exit 1
  fi
  echo "::endgroup::"
done
echo "guard-catalog: выполнено $count гвардий из $dir (пропущено $skipped)"
