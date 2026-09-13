#!/usr/bin/env bash
# Доказывает поведение путевого правила #1111 (`is_fixture_path` в
# provider-default.guard.sh) на РЕАЛЬНОМ файле гвардии (копируется как есть,
# не переписывается здесь заново) — не пересказ, а прогон в синтетическом
# дереве, тот же приём, что worktree-guard.test.sh. Три случая:
#
#   1) литерал прежнего дефолта в НЕКомментарийной строке ФИКСТУРЫ
#      (basename *.smoke.sh) — гвардия НЕ ловит (allowlist по пути, #1111);
#   2) тот же литерал, в том же виде, в файле, который путевому правилу
#      фикстурой не считается (обычный *.sh скрипт вне test/) — гвардия
#      ЛОВИТ (защита от реального стейл-литерала осталась);
#   3) property-паттерн (`${DEEPSEEK_MODEL:-glm-4}`) внутри ТОЙ ЖЕ фикстуры
#      из случая 1 — гвардия ВСЁ РАВНО ловит: путевое правило #1111 снимает
#      исключение только с литерального паттерна (б), не с property-паттерна
#      (а) — это и есть ответ на «второй риск» из задачи #1111 (property-
#      паттерн — про сам факт зашитого фолбэка, который от расположения
#      файла не зависит).
#
# Мутация, которой доказан этот тест (#1111): закомментируй строку
# `if is_fixture_path "$f"; then continue; fi` в цикле литерального паттерна
# provider-default.guard.sh — случай 1 начинает падать (фикстура снова
# ловится, зелёного файла из проверки CI-repo больше нет). Верни строку —
# тест снова зелёный.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
GUARD_SRC="$REPO_ROOT/scripts/lib/test/provider-default.guard.sh"

WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

fail=0
note() { echo "$@"; }

mkdir -p "$WORK/.github/workflows" "$WORK/docs/agents" "$WORK/scripts/lib/test"
cp "$GUARD_SRC" "$WORK/scripts/lib/test/provider-default.guard.sh"

# `$(...)` под `set -e` роняет скрипт ДО проверки кода возврата (класс,
# доказавший себя на worktree-guard.test.sh) — код возврата нужен именно
# отличный от нуля в случаях 2/3, поэтому захват — только через if/else,
# не через `var=$(cmd); code=$?` напрямую.
run_guard() {
  if out="$(cd "$WORK" && bash scripts/lib/test/provider-default.guard.sh 2>&1)"; then
    printf '%s\x1e0' "$out"
  else
    printf '%s\x1e1' "$out"
  fi
}
guard_out() { printf '%s' "${1%$'\x1e'*}"; }
guard_code() { printf '%s' "${1##*$'\x1e'}"; }

# ── случай 1: литерал в фикстуре (*.smoke.sh) — не должен ловиться ──────────
cat >"$WORK/scripts/lib/test/case1-fixture.smoke.sh" <<'EOF'
#!/usr/bin/env bash
echo "dsh: model glm-4 selected for this smoke run"
EOF

res1="$(run_guard)"; out1="$(guard_out "$res1")"; code1="$(guard_code "$res1")"
if [ "$code1" = 0 ]; then
  note "случай 1 (литерал в фикстуре *.smoke.sh): гвардия зелёная — ОК"
else
  note "случай 1 (литерал в фикстуре *.smoke.sh): гвардия КРАСНАЯ — ОШИБКА, фикстура не должна ловиться"
  printf '%s\n' "$out1" >&2
  fail=1
fi
rm -f "$WORK/scripts/lib/test/case1-fixture.smoke.sh"

# ── случай 2: тот же литерал, файл вне признаков фикстуры — должен ловиться ─
cat >"$WORK/scripts/prod-looking-config.sh" <<'EOF'
#!/usr/bin/env bash
echo "dsh: model glm-4 selected for this smoke run"
EOF

res2="$(run_guard)"; out2="$(guard_out "$res2")"; code2="$(guard_code "$res2")"
if [ "$code2" != 0 ] && printf '%s' "$out2" | grep -q "scripts/prod-looking-config.sh"; then
  note "случай 2 (тот же литерал вне фикстуры): гвардия КРАСНАЯ, называет файл — ОК"
else
  note "случай 2 (тот же литерал вне фикстуры): ОШИБКА, ожидался отказ гвардии с указанием файла"
  printf '%s\n' "$out2" >&2
  fail=1
fi
rm -f "$WORK/scripts/prod-looking-config.sh"

# ── случай 3: property-паттерн внутри той же фикстуры — должен ловиться ─────
cat >"$WORK/scripts/lib/test/case3-fixture.smoke.sh" <<'EOF'
#!/usr/bin/env bash
model="${DEEPSEEK_MODEL:-glm-4}"
echo "$model"
EOF

res3="$(run_guard)"; out3="$(guard_out "$res3")"; code3="$(guard_code "$res3")"
if [ "$code3" != 0 ] && printf '%s' "$out3" | grep -q "property-паттерн"; then
  note "случай 3 (фолбэк-фикстура рядом с DEEPSEEK_MODEL): гвардия КРАСНАЯ (property-паттерн) — ОК"
else
  note "случай 3 (фолбэк-фикстура рядом с DEEPSEEK_MODEL): ОШИБКА, property-паттерн обязан ловить и в фикстурах"
  printf '%s\n' "$out3" >&2
  fail=1
fi
rm -f "$WORK/scripts/lib/test/case3-fixture.smoke.sh"

# ── контроль: пустое дерево (только сам файл гвардии) — зелёное ─────────────
res0="$(run_guard)"; out0="$(guard_out "$res0")"; code0="$(guard_code "$res0")"
if [ "$code0" != 0 ]; then
  note "контроль (пустое дерево): гвардия КРАСНАЯ — ОШИБКА, ложное срабатывание вне сценариев"
  printf '%s\n' "$out0" >&2
  fail=1
fi

if [ "$fail" = 0 ]; then
  echo "provider-default-guard-allowlist: все случаи прошли как ожидалось"
fi
exit "$fail"
