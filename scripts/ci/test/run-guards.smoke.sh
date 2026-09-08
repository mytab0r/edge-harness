#!/usr/bin/env bash
# Smoke-прогон реального scripts/ci/run_guards.sh (#749) на синтетическом
# каталоге — не пересказ поведения, а исполнение прод-файла:
#   1) перебирает все *.sh каталога, каждый реально исполняется;
#   2) падение одной гвардии красит весь прогон (fail loud);
#   3) удаление файла каталога убирает соответствующую проверку из прогона
#      (критерий приёмки #749 — «мутация: удалить элемент источника —
#      проверка исчезает, а не остаётся исполняться из забытого места»);
#   4) пустой каталог красит CI явно, а не молча проходит нулём проверок.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
RUNNER_SRC="$REPO_ROOT/scripts/ci/run_guards.sh"

WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

fail=0
note() { echo "$@"; }

mkdir -p "$WORK/scripts/ci/guards"
cp "$RUNNER_SRC" "$WORK/scripts/ci/run_guards.sh"

# ── случай 1: два зелёных guard-файла — оба исполняются, оба видны в логе ──
cat > "$WORK/scripts/ci/guards/alpha.sh" <<'EOF'
#!/usr/bin/env bash
echo "alpha ran"
EOF
cat > "$WORK/scripts/ci/guards/beta.sh" <<'EOF'
#!/usr/bin/env bash
echo "beta ran"
EOF

out="$(cd "$WORK" && bash scripts/ci/run_guards.sh 2>&1)"
if echo "$out" | grep -q "guard: alpha" && echo "$out" | grep -q "alpha ran" \
  && echo "$out" | grep -q "guard: beta" && echo "$out" | grep -q "beta ran" \
  && echo "$out" | grep -q "guard-catalog: выполнено 2"; then
  note "OK: два guard-файла оба перебраны и исполнены"
else
  note "FAIL: перебор не нашёл/не исполнил оба guard-файла"
  echo "$out"
  fail=1
fi

# ── случай 2: падение одной гвардии красит весь прогон ─────────────────────
cat > "$WORK/scripts/ci/guards/broken.sh" <<'EOF'
#!/usr/bin/env bash
echo "broken about to fail"
exit 1
EOF

if (cd "$WORK" && bash scripts/ci/run_guards.sh) >/tmp/run-guards-broken.$$ 2>&1; then
  note "FAIL: сломанная гвардия не уронила прогон"
  cat /tmp/run-guards-broken.$$
  fail=1
else
  if grep -q "гвардия каталога 'broken'" /tmp/run-guards-broken.$$; then
    note "OK: сломанная гвардия названа по имени и уронила весь прогон"
  else
    note "FAIL: прогон упал, но без явного указания, какая гвардия виновата"
    cat /tmp/run-guards-broken.$$
    fail=1
  fi
fi
rm -f /tmp/run-guards-broken.$$
rm -f "$WORK/scripts/ci/guards/broken.sh"

# ── случай 3: мутация — удаление файла каталога убирает проверку из прогона ─
rm -f "$WORK/scripts/ci/guards/beta.sh"
out_after_removal="$(cd "$WORK" && bash scripts/ci/run_guards.sh 2>&1)"
if echo "$out_after_removal" | grep -q "guard: alpha" \
  && ! echo "$out_after_removal" | grep -q "guard: beta" \
  && echo "$out_after_removal" | grep -q "guard-catalog: выполнено 1"; then
  note "OK: удаление файла каталога убрало гвардию из прогона (не осталась исполняться из другого места)"
else
  note "FAIL: удалённая гвардия всё ещё как-то исполняется, либо счётчик не сходится"
  echo "$out_after_removal"
  fail=1
fi

# ── случай 4: пустой каталог красит CI явно, не проходит нулём молча ───────
rm -f "$WORK/scripts/ci/guards/alpha.sh"
if (cd "$WORK" && bash scripts/ci/run_guards.sh) >/tmp/run-guards-empty.$$ 2>&1; then
  note "FAIL: пустой каталог гвардий прошёл зелёным"
  cat /tmp/run-guards-empty.$$
  fail=1
else
  if grep -q "scripts/ci/guards пуст" /tmp/run-guards-empty.$$; then
    note "OK: пустой каталог красит CI явно"
  else
    note "FAIL: пустой каталог упал, но без объясняющего сообщения"
    cat /tmp/run-guards-empty.$$
    fail=1
  fi
fi
rm -f /tmp/run-guards-empty.$$

exit "$fail"
