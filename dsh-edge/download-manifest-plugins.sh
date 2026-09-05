#!/usr/bin/env bash
# Скачивает tarball'ы всех плагинов манифеста и сверяет sha256 — ОДНО место
# правды канала «релиз этого репо → локальный tgz» для двух потребителей:
#   * deploy-dsh-edge.yml — шаг «Скачать плагины и сверить sha256»;
#   * интеграционный дым plugin-forge.yml — клону нужны ВСЕ плагины
#     эффективного манифеста, а не только форжевый: кодогенератор рендерит
#     импорты всех серверных записей, гвардия патча 0003 требует клиентские
#     пакеты в node_modules, verify-edge-plugins — литералы состава и бандлы
#     (раньше дым ставил один тарболл и краснул на первом же чужом импорте).
# Форма «релиз ↔ реестр репо» проверяется здесь же (класс #115): релизный
# integrations.json сверяется с dsh-edge/integrations.json байт-в-байт, ростер
# релизного manifest.json — с dsh-edge/plugins.json; расхождение — красный
# шаг, а не зелёная сборка со старым составом.
#
# Использование: download-manifest-plugins.sh <dest-dir>
#   <dest-dir> — относительный (от cwd, обычно корень репо) или абсолютный
#   каталог; рядом с tgz остаются plugins.tsv / checksums.txt, а в
#   downloaded.txt — построчно пути tgz РОВНО как передавались (относительный
#   dest → относительные пути: шаг установки деплоя.prepend'ит
#   GITHUB_WORKSPACE сам).
# Env: GH_TOKEN (gh release download), GITHUB_REPOSITORY. Падает громко на
# первом несовпадении — частичный скачив молча проехать не может.
set -euo pipefail

[ $# = 1 ] || { echo "usage: $0 <dest-dir>" >&2; exit 2; }
DEST=$1
DSH_EDGE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p "$DEST"
manifest=$(node "$DSH_EDGE_DIR/manifest.mjs")
# Имена релизов, assets и ожидаемые sha256 — из валидированного манифеста
# (CLI manifest.mjs), не из сырого JSON и не из логов. Скачиваем ТОЛЬКО
# названные assets (--pattern), а не весь релиз: лишний tgz не имеет права
# проехать в pnpm add.
jq -r '.plugins[] | [.id, .release, .asset, .sha256] | @tsv' <<<"$manifest" > "$DEST/plugins.tsv"
: > "$DEST/checksums.txt"
: > "$DEST/downloaded.txt"
while IFS=$'\t' read -r id release asset sha; do
  echo "плагин $id: релиз $release, asset $asset"
  gh release download "$release" --repo "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY не задан}" \
    --dir "$DEST" --pattern "$asset" --clobber \
    || { echo "::error::Релиз $release недоступен — манифест ссылается на несуществующий источник"; exit 1; }
  [ -f "$DEST/$asset" ] || { echo "::error::В релизе $release нет asset'а $asset (плагин $id)"; exit 1; }
  echo "$sha  $DEST/$asset" >> "$DEST/checksums.txt"
  echo "$DEST/$asset" >> "$DEST/downloaded.txt"
  if tar -tf "$DEST/$asset" | grep -q '^package/integrations.json$'; then
    tar -xOf "$DEST/$asset" package/integrations.json \
      | diff -q - "$DSH_EDGE_DIR/integrations.json" >/dev/null \
      || { echo "::error::Реестр интеграций в релизе $asset разошёлся с dsh-edge/integrations.json — пересобери и перевыпусти плагин (конвейер #80)"; exit 1; }
  fi
  if tar -tf "$DEST/$asset" | grep -q '^package/manifest.json$'; then
    if ! diff \
      <(tar -xOf "$DEST/$asset" package/manifest.json | jq -S '[.plugins[] | {id, server, client}]') \
      <(jq -S '[.plugins[] | {id, server, client}]' "$DSH_EDGE_DIR/plugins.json") >/dev/null; then
      echo "::error::Ростер манифеста в релизе $asset разошёлся с dsh-edge/plugins.json — пересобери и перевыпусти плагин (конвейер #80)"; exit 1
    fi
  fi
done < "$DEST/plugins.tsv"
sha256sum -c "$DEST/checksums.txt" || { echo "::error::sha256 артефакта не сошёлся с манифестом — возможна подмена или перезапись релиза"; exit 1; }
