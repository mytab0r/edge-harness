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
#   3) property-паттерн (фолбэк-конструкция рядом с DEEPSEEK_MODEL) внутри
#      ТОЙ ЖЕ фикстуры из случая 1 — гвардия ВСЁ РАВНО ловит: путевое правило
#      #1111 снимает исключение только с литерального паттерна (б), не с
#      property-паттерна (а) — это и есть ответ на «второй риск» из задачи
#      #1111 (property-паттерн — про сам факт зашитого фолбэка, который от
#      расположения файла не зависит).
#   4) литерал в ФАЙЛЕ ДАННЫХ теста (путь через testdata/ — живой случай
#      #1172: корпус реальных цитат логов) — гвардия НЕ ловит, тот же
#      путевой признак, что у *.smoke.sh в случае 1;
#   5) property-паттерн внутри ТОГО ЖЕ файла данных testdata/ — гвардия
#      ВСЁ РАВНО ловит (case 3, применённый к случаю 4).
#
# Случай 3 собирает свою фикстуру через printf в рантайме (находка ai-review
# PR #1120), а не пишет фолбэк-конструкцию литералом в исходнике: этот файл
# сам лежит в SCOPE гвардии (scripts/**), и property-паттерн (а) не исключает
# по пути НИКОГО, включая тестовые файлы (см. обоснование выше) — литерал
# рядом с DEEPSEEK_MODEL в исходнике самого теста красил бы собственный
# обязательный шаг CI (repo-ci.yml, provider-latency-bench.yml). Тот же
# приём, каким сама гвардия избегает самопоражения — SELF-исключение по
# имени; здесь конструкция вместо имени, потому что этот файл не гвардия,
# а её потребитель, и не должен расти в списке ручных исключений.
#
# Мутация, которой доказан этот тест (#1111): закомментируй строку
# `if is_fixture_path "$f"; then continue; fi` в цикле литерального паттерна
# provider-default.guard.sh — случаи 1 и 4 начинают падать (фикстуры снова
# ловятся, зелёного файла из проверки CI-repo больше нет). Возврат строк —
# тесты снова зелёные. Мутация для путевой половины `*/testdata/*` (issue
# #1172): убери `|*/testdata/*` из case в is_fixture_path — падает случай 4.
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
# Имя переменной — отдельным токеном (не рядом с ":-" в исходнике ЭТОГО
# файла) и подставляется printf'ом только в РАНТАЙМЕ, в фикстуру на диске:
# см. обоснование в docstring выше.
model_var='DEEPSEEK_MODEL'
{
  echo '#!/usr/bin/env bash'
  printf 'model="${%s:-glm-4}"\n' "$model_var"
  echo 'echo "$model"'
} >"$WORK/scripts/lib/test/case3-fixture.smoke.sh"

res3="$(run_guard)"; out3="$(guard_out "$res3")"; code3="$(guard_code "$res3")"
if [ "$code3" != 0 ] && printf '%s' "$out3" | grep -q "property-паттерн"; then
  note "случай 3 (фолбэк-фикстура рядом с DEEPSEEK_MODEL): гвардия КРАСНАЯ (property-паттерн) — ОК"
else
  note "случай 3 (фолбэк-фикстура рядом с DEEPSEEK_MODEL): ОШИБКА, property-паттерн обязан ловить и в фикстурах"
  printf '%s\n' "$out3" >&2
  fail=1
fi
rm -f "$WORK/scripts/lib/test/case3-fixture.smoke.sh"

# ── случай 4: литерал в ФАЙЛЕ ДАННЫХ теста (testdata/) — не должен ловиться ──
# Живой случай #1172: корпус реальных цитат логов — его назначение нести
# чужой литерал байт-в-байт (real_error_log_corpus.txt, «nemotron-3-ultra»).
mkdir -p "$WORK/scripts/orchestra/testdata"
cat >"$WORK/scripts/orchestra/testdata/case4-real-log-corpus.txt" <<'EOF'
dsh: INVALID_REQUEST: max_tokens (131072) exceeds model's maximum output tokens (65536) for model nemotron-3-ultra
EOF

res4="$(run_guard)"; out4="$(guard_out "$res4")"; code4="$(guard_code "$res4")"
if [ "$code4" = 0 ]; then
  note "случай 4 (литерал в файле данных testdata/): гвардия зелёная — ОК"
else
  note "случай 4 (литерал в файле данных testdata/): гвардия КРАСНАЯ — ОШИБКА, файл данных теста не должен ловиться литеральным паттерном"
  printf '%s\n' "$out4" >&2
  fail=1
fi

# ── случай 5: property-паттерн в том же файле данных testdata/ — должен ловиться
{
  echo '#!/usr/bin/env bash'
  printf 'model="${%s:-glm-4}"\n' "$model_var"
  echo 'echo "$model"'
} >"$WORK/scripts/orchestra/testdata/case5-data-with-fallback.txt"

res5="$(run_guard)"; out5="$(guard_out "$res5")"; code5="$(guard_code "$res5")"
if [ "$code5" != 0 ] && printf '%s' "$out5" | grep -q "property-паттерн"; then
  note "случай 5 (фолбэк в файле данных testdata/): гвардия КРАСНАЯ (property-паттерн) — ОК"
else
  note "случай 5 (фолбэк в файле данных testdata/): ОШИБКА, property-паттерн обязан ловить и в testdata/"
  printf '%s\n' "$out5" >&2
  fail=1
fi
rm -rf "$WORK/scripts/orchestra/testdata"

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
