#!/usr/bin/env bash
# Гвардия класса #876 (живой инцидент — прогон worker.yml 34498185823, задача
# #140, 2026-09-10): worker/task.sh отрапортовал «🤖 Автономный воркер
# справился (провайдер: ?)», хотя dsh упал на ВСЕХ провайдерах цепочки этого
# прогона (rc=1, WORKER_CHAIN_PROVIDER пусто) — критерий успеха смотрел
# ТОЛЬКО на факт «PR по ветке существует» (pr_outcome.py), а PR #395
# существовал с прошлого прогона (2026-09-05).
#
# Тестирует dsh_worker_run_is_success (scripts/lib/dsh-ci.sh) — ЕДИНСТВЕННОЕ
# место правды критерия «работа этого прогона доказана» (rc==0 И непустой
# провайдер И новый коммит в ветке), от которого worker/task.sh требует
# конъюнкции с pr_outcome.py (существование PR), а не решает по одному ему.
#
# Мутация, которой доказана проверка (см. НИЗ файла): если
# dsh_worker_run_is_success игнорирует rc/provider/sha и возвращает 0 всегда
# (старое поведение, эквивалент «единственный критерий — pr_outcome_rc»),
# сценарий 2 (живая форма инцидента) обязан покраснеть.
#
# Запуск: bash scripts/lib/test/dsh-worker-success-gate.smoke.sh
set -euo pipefail

SMOKE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SMOKE_DIR/../../.." && pwd)"

fail() { echo "::error::SMOKE(worker-success-gate): $*" >&2; exit 1; }

# shellcheck source=scripts/lib/dsh-ci.sh
source "$REPO/scripts/lib/dsh-ci.sh"

SAME_SHA="7d587bcfaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
NEW_SHA="1234567890bbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

# ── 1) успех: rc=0, провайдер назван, есть новый коммит ──────────────────────
if dsh_worker_run_is_success 0 "GLM" "$SAME_SHA" "$NEW_SHA"; then
  echo "SMOKE(worker-success-gate): 1) rc=0 + провайдер + новый коммит -> успех — ок"
else
  fail "1) настоящий успех этого прогона обязан пройти гейт"
fi

# ── 2) живая форма инцидента #876: rc=1, провайдер пуст, ветка НЕ сдвинулась ──
# (PR #395 существовал с прошлого прогона — WORKER_BRANCH_START_SHA ==
# WORKER_BRANCH_END_SHA, ни одного нового коммита в этом прогоне).
if dsh_worker_run_is_success 1 "" "$SAME_SHA" "$SAME_SHA"; then
  fail "2) живая форма инцидента #876 обязана провалить гейт (rc=1, провайдер пуст, коммитов нет)"
else
  echo "SMOKE(worker-success-gate): 2) rc=1 + пустой провайдер + предсуществующая ветка -> провал гейта — ок"
fi

# ── 3) rc=0 без имени провайдера (защитный случай — рассинхрон переменных) ───
if dsh_worker_run_is_success 0 "" "$SAME_SHA" "$NEW_SHA"; then
  fail "3) пустой провайдер при rc=0 — противоречие, обязано быть провалом гейта"
else
  echo "SMOKE(worker-success-gate): 3) rc=0 но провайдер не назван -> провал гейта — ок"
fi

# ── 4) rc=0, провайдер назван, но ветка НЕ сдвинулась (нет новых коммитов) ───
# DSH мог решить, что PR уже полон, и не запушить ничего нового — playbook
# требует запушить исправление в ту же ветку, поэтому это тоже не успех.
if dsh_worker_run_is_success 0 "GLM" "$SAME_SHA" "$SAME_SHA"; then
  fail "4) rc=0 без единого нового коммита в ветке — не доказательство работы этого прогона"
else
  echo "SMOKE(worker-success-gate): 4) rc=0 + провайдер, но без новых коммитов -> провал гейта — ок"
fi

# ── 5) rc!=0, провайдер назван (не должно случаться, но гейт обязан быть строг) ─
if dsh_worker_run_is_success 1 "GLM" "$SAME_SHA" "$NEW_SHA"; then
  fail "5) rc!=0 не может быть успехом, даже если провайдер назван"
else
  echo "SMOKE(worker-success-gate): 5) rc!=0 -> провал гейта независимо от провайдера — ок"
fi

# ── 6) #880: DSH_WORKER_RUN_GATE_GAPS — одно место правды на текст причины,
# не вторая копия трёх условий в task.sh. Живая форма инцидента #876
# (сценарий 2) обязана назвать ВСЕ три несработавших конъюнкта; успех —
# оставить переменную пустой.
dsh_worker_run_is_success 1 "" "$SAME_SHA" "$SAME_SHA" || true
case "$DSH_WORKER_RUN_GATE_GAPS" in
  *"кодом 1"*"ни один провайдер"*"новых коммитов"*) ;;
  *) fail "6) DSH_WORKER_RUN_GATE_GAPS обязан назвать все три несработавших конъюнкта живого инцидента: '$DSH_WORKER_RUN_GATE_GAPS'" ;;
esac
echo "SMOKE(worker-success-gate): 6) DSH_WORKER_RUN_GATE_GAPS называет все несработавшие конъюнкты — ок"

dsh_worker_run_is_success 0 "GLM" "$SAME_SHA" "$NEW_SHA" || true
[ -z "$DSH_WORKER_RUN_GATE_GAPS" ] \
  || fail "6) успех обязан оставить DSH_WORKER_RUN_GATE_GAPS пустым, получено: '$DSH_WORKER_RUN_GATE_GAPS'"
echo "SMOKE(worker-success-gate): 6b) успех оставляет DSH_WORKER_RUN_GATE_GAPS пустым — ок"

echo "SMOKE(worker-success-gate): критерий успеха воркера (#876) держит все пять сценариев — гвардия зелёная"
