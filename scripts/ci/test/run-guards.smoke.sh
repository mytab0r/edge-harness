#!/usr/bin/env bash
# Smoke-прогон реального scripts/ci/run_guards.sh (#749) на синтетическом
# каталоге — не пересказ поведения, а исполнение прод-файла:
#   1) перебирает все *.sh каталога, каждый реально исполняется;
#   2) падение одной гвардии красит весь прогон (fail loud);
#   3) удаление файла каталога убирает соответствующую проверку из прогона
#      (критерий приёмки #749 — «мутация: удалить элемент источника —
#      проверка исчезает, а не остаётся исполняться из забытого места»);
#   4) пустой каталог красит CI явно, а не молча проходит нулём проверок;
#   5) файл вне соглашения '*.sh' верхнего уровня (другое расширение/регистр,
#      без расширения, поддиректория) не теряется молча (#749, ревью PR #771,
#      блокирующая 3);
#   6) файл-пустышка (0 байт или только шебанг/`set -euo pipefail`) не
#      регистрируется как прошедшая проверка (та же блокирующая 3);
#   7) отсутствующий каталог scripts/ci/guards красит CI явно (minor 12).
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

broken_out="$WORK/run-guards-broken.out"
if (cd "$WORK" && bash scripts/ci/run_guards.sh) >"$broken_out" 2>&1; then
  note "FAIL: сломанная гвардия не уронила прогон"
  cat "$broken_out"
  fail=1
else
  if grep -q "гвардия каталога 'broken'" "$broken_out"; then
    note "OK: сломанная гвардия названа по имени и уронила весь прогон"
  else
    note "FAIL: прогон упал, но без явного указания, какая гвардия виновата"
    cat "$broken_out"
    fail=1
  fi
fi
rm -f "$broken_out"
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

# ── случай 5: записи вне соглашения '*.sh' верхнего уровня не теряются молча ─
# Живые прогоны гейта (ревью PR #771, блокирующая 3): каждая из этих пяти
# записей раньше давала EXIT=0 и заниженный счётчик «выполнено N» вместо
# явного отказа. Проверяем каждую ПООЧЕРЕДНО (не все разом), чтобы отказ на
# первой не маскировал остальные четыре.
declare -a unexpected_cases=(
  "important-guard.bash:#!/usr/bin/env bash\necho important\nexit 1\n"
  "noext:#!/usr/bin/env bash\necho important\nexit 1\n"
  "UPPER-GUARD.SH:#!/usr/bin/env bash\necho important\nexit 1\n"
  "stray.py:print('not a bash guard')\n"
)
for case_entry in "${unexpected_cases[@]}"; do
  case_name="${case_entry%%:*}"
  case_body="${case_entry#*:}"
  printf '%b' "$case_body" > "$WORK/scripts/ci/guards/$case_name"
  case_out="$WORK/run-guards-unexpected.out"
  if (cd "$WORK" && bash scripts/ci/run_guards.sh) >"$case_out" 2>&1; then
    note "FAIL: запись вне соглашения '$case_name' прошла зелёным (молча не зарегистрирована)"
    cat "$case_out"
    fail=1
  elif ! grep -q "вне соглашения" "$case_out"; then
    note "FAIL: запись '$case_name' уронила прогон без объясняющего сообщения"
    cat "$case_out"
    fail=1
  else
    note "OK: запись вне соглашения '$case_name' красит CI явно, а не теряется молча"
  fi
  rm -f "$case_out" "$WORK/scripts/ci/guards/$case_name"
done

# Вложенная поддиректория с *.sh внутри — тоже запись верхнего уровня вне
# соглашения (сама поддиректория не совпадает с шаблоном `*.sh`).
mkdir -p "$WORK/scripts/ci/guards/orchestra"
cat > "$WORK/scripts/ci/guards/orchestra/nested-guard.sh" <<'EOF'
#!/usr/bin/env bash
echo important
exit 1
EOF
nested_out="$WORK/run-guards-nested.out"
if (cd "$WORK" && bash scripts/ci/run_guards.sh) >"$nested_out" 2>&1; then
  note "FAIL: вложенная scripts/ci/guards/orchestra/nested-guard.sh прошла зелёным (молча не зарегистрирована)"
  cat "$nested_out"
  fail=1
elif ! grep -q "вне соглашения" "$nested_out"; then
  note "FAIL: вложенный каталог уронил прогон без объясняющего сообщения"
  cat "$nested_out"
  fail=1
else
  note "OK: вложенная поддиректория каталога красит CI явно, а не теряется молча"
fi
rm -f "$nested_out"
rm -rf "$WORK/scripts/ci/guards/orchestra"

# ── случай 6: файл-пустышка (0 байт / только шебанг+set) не регистрируется ──
declare -a empty_cases=(
  "zero-bytes.sh:"
  "shebang-only.sh:#!/usr/bin/env bash\nset -euo pipefail\n"
)
for case_entry in "${empty_cases[@]}"; do
  case_name="${case_entry%%:*}"
  case_body="${case_entry#*:}"
  printf '%b' "$case_body" > "$WORK/scripts/ci/guards/$case_name"
  case_out="$WORK/run-guards-empty-file.out"
  if (cd "$WORK" && bash scripts/ci/run_guards.sh) >"$case_out" 2>&1; then
    note "FAIL: файл-пустышка '$case_name' прошёл зелёным как будто он гвардия"
    cat "$case_out"
    fail=1
  elif ! grep -q "не несёт ни одной команды" "$case_out"; then
    note "FAIL: файл-пустышка '$case_name' уронил прогон без объясняющего сообщения"
    cat "$case_out"
    fail=1
  else
    note "OK: файл-пустышка '$case_name' красит CI явно, а не считается прошедшей проверкой"
  fi
  rm -f "$case_out" "$WORK/scripts/ci/guards/$case_name"
done

# ── случай 4: пустой каталог красит CI явно, не проходит нулём молча ───────
rm -f "$WORK/scripts/ci/guards/alpha.sh"
empty_dir_out="$WORK/run-guards-empty.out"
if (cd "$WORK" && bash scripts/ci/run_guards.sh) >"$empty_dir_out" 2>&1; then
  note "FAIL: пустой каталог гвардий прошёл зелёным"
  cat "$empty_dir_out"
  fail=1
else
  if grep -q "scripts/ci/guards пуст" "$empty_dir_out"; then
    note "OK: пустой каталог красит CI явно"
  else
    note "FAIL: пустой каталог упал, но без объясняющего сообщения"
    cat "$empty_dir_out"
    fail=1
  fi
fi
rm -f "$empty_dir_out"

# ── случай 7: каталог scripts/ci/guards вовсе не существует ────────────────
rmdir "$WORK/scripts/ci/guards"
missing_dir_out="$WORK/run-guards-missing-dir.out"
if (cd "$WORK" && bash scripts/ci/run_guards.sh) >"$missing_dir_out" 2>&1; then
  note "FAIL: отсутствующий каталог scripts/ci/guards прошёл зелёным"
  cat "$missing_dir_out"
  fail=1
elif ! grep -q "scripts/ci/guards не существует" "$missing_dir_out"; then
  note "FAIL: отсутствующий каталог упал без объясняющего сообщения"
  cat "$missing_dir_out"
  fail=1
else
  note "OK: отсутствующий каталог scripts/ci/guards красит CI явно"
fi
rm -f "$missing_dir_out"

exit "$fail"
