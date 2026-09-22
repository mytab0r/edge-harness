#!/usr/bin/env bash
# Крах инструмента не выдаётся за отказ провайдеров (#1470).
#
# Класс одной фразой: **один локальный отказ, отпечатанный по числу
# провайдеров, считался N отказами провайдеров, и итог советовал повтор,
# который не может помочь.**
#
# Класс стоил репозиторию дважды, и оба раза совет был заведомо пустым:
#   #1315 — промпт не влез в аргумент (E2BIG), восемь «отказов» за 78 секунд
#           без единого обращения к сети; починили СЛУЧАЙ, класс остался;
#   #1467 — транзитивная зависимость dsh обновилась в чужом реестре, dsh стал
#           падать на старте за 0–1 секунду; итог снова «повторить прогон».
#
# Стенд зовёт НАСТОЯЩУЮ функцию из dsh-ci.sh на прод-форме данных: строки
# DSH_CHAIN_OUTCOMES ровно того вида, что пишет `_dsh_chain_record_outcome`,
# включая дословную выдержку stderr из прогона 35752743682. Текстовый разбор
# исходника вместо запуска здесь был бы бесполезен: вырезанное тело проверки
# прошло бы его молча (AGENTS.md, «Поведенческий тест находит то, чего
# структурный не видит»).
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
fail() { echo "::error::GUARD(dsh-tool-crash-class): $*" >&2; exit 1; }

# Дословная выдержка из живого прогона ai-review 35752743682 (#1467).
CRASH_NOTE='класс не распознан (file:///opt/hostedtoolcache/node/24.20.0/x64/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-app-boot/lib/index.js:764 	if (hmr === void 0) throw new Error(`${binName}: user patch-laye)'

report() { # $1 — содержимое DSH_CHAIN_OUTCOMES, $2 — total
  (
    # shellcheck disable=SC1091
    source "$repo_root/scripts/lib/dsh-ci.sh"
    DSH_CHAIN_OUTCOMES="$1"
    DSH_CHAIN_TRIED="GLM, OpenRouter-2"
    DSH_CHAIN_RESET_HINT=""
    _dsh_chain_report_exhausted "$2" 2>&1
    printf 'RETRY_USEFUL=%s\n' "${DSH_CHAIN_RETRY_USEFUL:-?}"
    printf 'REASON=%s\n' "${DSH_RUN_FAILURE_REASON:-}"
  )
}

# 1. Живой случай #1467: все опробованные отказали ОДИНАКОВО.
outcomes=$(printf 'GLM\ttransient\t%s\nOpenRouter-2\ttransient\t%s\n' "$CRASH_NOTE" "$CRASH_NOTE")
out=$(report "$outcomes" 2)
grep -qF -- "инструмент не стартовал" <<<"$out" \
  || fail "1) одинаковый локальный отказ не распознан: $out"
grep -qF -- "RETRY_USEFUL=0" <<<"$out" \
  || fail "1) флаг повтора остался поднятым — конвейер будет перезапускать вечно: $out"
grep -qF -- "REASON=tool_did_not_start" <<<"$out" \
  || fail "1) причина не машиночитаема: $out"
grep -qvF -- "повторить прогон" <<<"$out" \
  || fail "1) совет «повторить прогон» остался — он не может помочь: $out"
echo "GUARD(dsh-tool-crash-class): 1) одинаковый отказ у всех -> «инструмент не стартовал», повтор не советуется"

# 2. Настоящие разные отказы провайдеров — поведение прежнее, повтор осмыслен.
outcomes=$(printf 'GLM\ttransient\tHTTP_502 от api.z.ai\nOpenRouter-2\ttransient\tтаймаут соединения\n')
out=$(report "$outcomes" 2)
grep -qF -- "повторить прогон" <<<"$out" \
  || fail "2) разные транзиентные отказы перестали советовать повтор — сломана прежняя семантика: $out"
grep -qF -- "RETRY_USEFUL=1" <<<"$out" || fail "2) флаг повтора не поднят: $out"
grep -qvF -- "инструмент не стартовал" <<<"$out" \
  || fail "2) разные отказы выданы за крах инструмента: $out"
echo "GUARD(dsh-tool-crash-class): 2) разные отказы -> прежний путь, повтор осмыслен"

# 3. Одинаковая выдержка, но опробован ОДИН провайдер: доказательства нет.
#    Один отказ не отличает «наш» от «их», и объявлять крах инструмента по
#    одной точке значило бы гадать — ровно то, что правило запрещает.
outcomes=$(printf 'GLM\ttransient\t%s\n' "$CRASH_NOTE")
out=$(report "$outcomes" 1)
grep -qvF -- "инструмент не стартовал" <<<"$out" \
  || fail "3) вердикт вынесен по одной точке: $out"
echo "GUARD(dsh-tool-crash-class): 3) один провайдер -> вердикт не выносится"

# 4. Среди исходов есть НЕ transient (например настоящая квота) — значит
#    провайдеры отвечали, и общий крах инструмента исключён.
outcomes=$(printf 'GLM\tquota\tсброс завтра\nOpenRouter-2\ttransient\t%s\n' "$CRASH_NOTE")
out=$(report "$outcomes" 2)
grep -qvF -- "инструмент не стартовал" <<<"$out" \
  || fail "4) смешанные классы выданы за крах инструмента: $out"
echo "GUARD(dsh-tool-crash-class): 4) смешанные классы -> вердикт не выносится"

# 5. Пустая выдержка ничего не доказывает: совпадение пустот — не признак.
outcomes=$(printf 'GLM\ttransient\t\nOpenRouter-2\ttransient\t\n')
out=$(report "$outcomes" 2)
grep -qvF -- "инструмент не стартовал" <<<"$out" \
  || fail "5) вердикт вынесен по совпадению ПУСТЫХ выдержек: $out"
echo "GUARD(dsh-tool-crash-class): 5) пустые выдержки -> вердикт не выносится"

echo "GUARD(dsh-tool-crash-class): крах инструмента отделён от отказа провайдеров (#1470) — гвардия зелёная"
