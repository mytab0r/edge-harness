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
# несутся так: GH_TOKEN — общим `env:` уровня job `test` (см. repo-ci.yml,
# каждый файл каталога читает переменную напрямую); условие
# `github.event_name == 'push'` — сам файл гвардии читает $GITHUB_EVENT_NAME
# (штатная переменная окружения раннера GitHub Actions, доступна без
# дополнительной проводки). Единственный НЕ перенесённый случай — шаг,
# чьё `if:` зависит от OUTPUT другого шага этого же job (`steps.quota.
# outputs.skip`, гейт квоты перед инвариантами репозитория): такая
# зависимость специфична для одной пары шагов и не обобщается на каталог
# без спекулятивной инфраструктуры (design.md #749 называет это явно, не
# молчит) — соответствующий шаг остаётся рукописным в repo-ci.yml.
set -euo pipefail
shopt -s nullglob

dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/guards" && pwd)"
scripts=("$dir"/*.sh)

if [ "${#scripts[@]}" -eq 0 ]; then
  echo "::error::scripts/ci/guards пуст — перебор не нашёл ни одного файла (пустой каталог красит CI, а не молча проходит нулём проверок)"
  exit 1
fi

count=0
for script in "${scripts[@]}"; do
  name="$(basename "$script" .sh)"
  echo "::group::guard: $name"
  count=$((count + 1))
  if ! bash "$script"; then
    echo "::endgroup::"
    echo "::error::гвардия каталога '$name' (scripts/ci/guards/$name.sh) провалилась"
    exit 1
  fi
  echo "::endgroup::"
done
echo "guard-catalog: выполнено $count гвардий из $dir"
