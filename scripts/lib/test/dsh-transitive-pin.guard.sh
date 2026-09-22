#!/usr/bin/env bash
# Транзитивный пин dsh проверяется по диску, а не по намерению (#1467).
#
# Класс одной фразой: **пины держат два верхних пакета, а их зависимости
# резолвятся каждый прогон заново — публикация в ЧУЖОМ реестре меняет то, что
# стоит у нас, без единого коммита.** Живой инцидент 2026-09-22:
# `@deepseek-ai/cordis` 4.0.4 (опубликован 15:36:40Z) попал в диапазон
# `^4.0.1` пакета `dsh@0.1.1-rc.2`, и `dsh` начал падать с
# «user patch-layer watching requires the Cordis HMR service» ДО обращения к
# провайдеру — конвейер слияний встал: ни один PR не мог получить `ai:ok`.
#
# `--before` — просьба к npm. Доказательство — версия на диске, и его даёт
# `dsh_verify_resolved_deps`. Этот стенд проверяет саму проверку: подменяет
# `npm root -g` временным каталогом и смотрит ПОВЕДЕНИЕ на трёх состояниях —
# версия та, версия уехала, манифеста нет. Структурный тест (grep по
# исходнику) здесь бесполезен: он зеленел бы и на вырезанном теле функции
# (AGENTS.md, «Поведенческий тест находит то, чего структурный не видит»).
#
# Запуск: bash scripts/lib/test/dsh-transitive-pin.guard.sh
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

fail() { echo "::error::GUARD(dsh-transitive-pin): $*" >&2; exit 1; }

# Стенд: настоящий каталог с настоящим package.json, который читает настоящий
# node внутри проверяемой функции — не пересказ её логики.
make_root() { # $1 — версия cordis, пусто = манифеста нет вовсе
  local root="$tmp/npmroot-$RANDOM"
  mkdir -p "$root/@deepseek-ai/cordis"
  if [ -n "${1:-}" ]; then
    printf '{"name":"@deepseek-ai/cordis","version":"%s"}\n' "$1" \
      >"$root/@deepseek-ai/cordis/package.json"
  fi
  printf '%s' "$root"
}

run_check() { # $1 — каталог, который вернёт подменённый `npm root -g`
  # Имя переменной НЕ `root`: у bash динамическая область видимости, и
  # `local root` внутри проверяемой функции перекрыл бы переменную стенда —
  # заглушка вернула бы пустую строку, а стенд «нашёл» бы несуществующий
  # дефект. Поймано исполнением на первом же прогоне.
  STUB_NPM_ROOT=$1
  (
    # shellcheck disable=SC1091
    source "$repo_root/scripts/lib/dsh-ci.sh"
    npm() { [ "${1:-}" = "root" ] && { printf '%s\n' "$STUB_NPM_ROOT"; return 0; }; command npm "$@"; }
    dsh_verify_resolved_deps
  )
}

# 1. Версия совпадает с пином — проверка молчит и выходит нулём.
root_ok=$(make_root "4.0.3")
out=$(run_check "$root_ok" 2>&1) || fail "1) ожидаемая версия отвергнута: $out"
grep -qF -- "4.0.3" <<<"$out" || fail "1) вывод не называет установленную версию: $out"
echo "GUARD(dsh-transitive-pin): 1) версия совпала с пином -> ок"

# 2. Версия уехала — ровно тот инцидент, ради которого заведена проверка.
root_drift=$(make_root "4.0.4")
if out=$(run_check "$root_drift" 2>&1); then
  fail "2) уехавшая транзитивная зависимость прошла молча — это и есть #1467: $out"
fi
grep -qF -- "4.0.4" <<<"$out" || fail "2) сообщение не называет ФАКТИЧЕСКУЮ версию: $out"
grep -qF -- "4.0.3" <<<"$out" || fail "2) сообщение не называет ОЖИДАЕМУЮ версию: $out"
grep -qF -- "#1467" <<<"$out" || fail "2) сообщение не даёт адрес разбора: $out"
echo "GUARD(dsh-transitive-pin): 2) уехавшая версия -> громкий отказ с обоими числами"

# 3. Манифеста нет — «проверить нечем» это отдельное сообщение, не тихий ноль
#    и не «версия не та» (AGENTS.md: «возможности нет» и «возможность есть, но
#    сломана» лечатся по-разному).
root_missing=$(make_root "")
if out=$(run_check "$root_missing" 2>&1); then
  fail "3) отсутствие манифеста прошло как успех — silent-wrong: $out"
fi
grep -qF -- "не найден манифест" <<<"$out" \
  || fail "3) отсутствие манифеста не отличено от расхождения версий: $out"
echo "GUARD(dsh-transitive-pin): 3) манифеста нет -> своё сообщение, не «версия не та»"

# 4. Дата пина и ожидаемая версия объявлены рядом и непусты: разъехавшись, они
#    дают проверку, которая формально зелёная и ничего не держит.
(
  # shellcheck disable=SC1091
  source "$repo_root/scripts/lib/dsh-ci.sh"
  [ -n "${DSH_RESOLVE_BEFORE:-}" ] || { echo "DSH_RESOLVE_BEFORE пуст"; exit 1; }
  [ -n "${DSH_EXPECTED_CORDIS:-}" ] || { echo "DSH_EXPECTED_CORDIS пуст"; exit 1; }
  grep -qE '^--before=|^[0-9]{4}-[0-9]{2}-[0-9]{2}T' <<<"$DSH_RESOLVE_BEFORE" \
    || { echo "DSH_RESOLVE_BEFORE не похож на дату: $DSH_RESOLVE_BEFORE"; exit 1; }
) || fail "4) пины транзитивного резолва объявлены неполно"
echo "GUARD(dsh-transitive-pin): 4) дата пина и ожидаемая версия объявлены -> ок"

# 5. Установка действительно просит npm о пине — иначе проверка на диске будет
#    вечно ловить дрейф вместо того, чтобы его не допускать.
grep -qF -- 'npm pack --before="$DSH_RESOLVE_BEFORE"' "$repo_root/scripts/lib/dsh-ci.sh" \
  || fail "5) npm pack без --before: транзитивные версии снова резолвятся «как сегодня»"
grep -qF -- 'npm install -g --before="$DSH_RESOLVE_BEFORE"' "$repo_root/scripts/lib/dsh-ci.sh" \
  || fail "5) npm install без --before: то же самое на шаге установки"
echo "GUARD(dsh-transitive-pin): 5) обе команды npm несут --before -> ок"

echo "GUARD(dsh-transitive-pin): транзитивный пин dsh (#1467) — гвардия зелёная"
