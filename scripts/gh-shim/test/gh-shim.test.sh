#!/usr/bin/env bash
# Мутационная проверка шима gh (#594): физическая невозможность голого
# `gh pr create` в среде воркера, прозрачность для всех остальных подкоманд,
# отсутствие рекурсии, громкий отказ при сломанной установке.
#
# Важно: команды ниже вызывают `gh` КАК ИМЯ КОМАНДЫ (bare, через PATH-lookup
# самого bash), а не путём scripts/gh-shim/gh напрямую — иначе тест доказывал
# бы только «файл шима сам по себе умеет отклонять», а не «шим встаёт в PATH
# раньше настоящего gh и агент физически не может его обойти», что и есть
# предмет задачи #594.
#
# Мутация, которой доказана проверка (случай 5 ниже — «шим снят из PATH»):
# ровно тот же вызов, что заблокирован в случае 1, при отсутствии каталога
# шима в PATH обязан ДОСТУЧАТЬСЯ до настоящего gh — если бы случай 1 сам по
# себе был устроен так, что блокировка происходит НЕ из-за PATH (например,
# случайно захардкожен путь к шиму), случай 5 покраснел бы, доказывая, что
# случай 1 был бы ложно-зелёным без реальной защиты.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

fail=0
note() { echo "$@"; }

# ── «Настоящий» gh для теста: не сеть, доказывает байт-в-байт прозрачность ──
# argv/stdin/stdout/stderr/exit code — вызван ли напрямую или через шим.
mkdir -p "$WORK/real-bin"
MARKER="$WORK/real-gh-pr-create-called"
cat >"$WORK/real-bin/gh" <<'REALGH'
#!/usr/bin/env bash
if [ "${1:-}" = "pr" ] && [ "${2:-}" = "create" ]; then
  echo called >"${REALGH_MARKER:?REALGH_MARKER не задан}"
  printf 'https://github.test/o/r/pull/1\n'
  exit 0
fi
if [ "${1:-}" = "api" ]; then
  body="$(cat)"
  printf 'API-ARGV:%s\n' "$*"
  printf 'API-STDIN:%s\n' "$body"
  echo "предупреждение по-русски (кириллица, не ascii)" >&2
  exit "${REALGH_EXIT:-0}"
fi
if [ "${1:-}" = "pr" ] && [ "${2:-}" = "view" ]; then
  printf '{"number":%s,"title":"тест"}\n' "${3:-0}"
  exit 0
fi
if [ "${1:-}" = "issue" ] && [ "${2:-}" = "list" ]; then
  printf '[]\n'
  exit 0
fi
if [ "${1:-}" = "run" ] && [ "${2:-}" = "list" ]; then
  printf '[]\n' >&2   # намеренно в stderr — проверяем, что потоки не перепутаны
  exit "${REALGH_EXIT:-0}"
fi
printf 'UNKNOWN:%s\n' "$*"
exit 99
REALGH
chmod +x "$WORK/real-bin/gh"
export REALGH_MARKER="$MARKER"

source "$REPO_ROOT/scripts/lib/gh_shim.sh"

# PATH до установки: только «настоящий» gh теста — как в раннере ДО того, как
# worker/task.sh поставил шим.
BASE_PATH="$WORK/real-bin:$PATH"

# ── установка ────────────────────────────────────────────────────────────────
export PATH="$BASE_PATH"
if ! gh_shim_install "$WORK/gh-shim" >"$WORK/install.log" 2>&1; then
  note "FAIL установка: gh_shim_install вернул ошибку"; cat "$WORK/install.log"; fail=1
fi
if [ "$(type -P gh)" != "$WORK/gh-shim/gh" ]; then
  note "FAIL установка: type -P gh → $(type -P gh), ожидался $WORK/gh-shim/gh"; fail=1
else
  note "OK установка: type -P gh указывает на шим"
fi

# ── случай 1: gh pr create отклонён ДО настоящего gh ─────────────────────────
rm -f "$MARKER"
if out=$(gh pr create --title t --body-file "$WORK/body.md" 2>"$WORK/err1"); then
  note "FAIL случай 1: gh pr create принят шимом (ожидался отказ): $out"; fail=1
elif [ -f "$MARKER" ]; then
  note "FAIL случай 1: настоящий gh был вызван, хотя pr create обязан быть отклонён шимом"; fail=1
elif ! grep -q "scripts/git/pr-create" "$WORK/err1"; then
  note "FAIL случай 1: в stderr нет отсылки к scripts/git/pr-create"; cat "$WORK/err1"; fail=1
else
  note "OK случай 1: gh pr create отклонён ДО сети, настоящий gh не вызван"
fi

# ── случай 2: прозрачность — gh api (argv, exit code, ОБА потока, stdin) ────
direct_out=$(REALGH_EXIT=7 "$WORK/real-bin/gh" api repos/o/r/pulls/1 --jq .number <<<"тело-запроса" 2>"$WORK/direct.err") && direct_rc=0 || direct_rc=$?
shim_out=$(REALGH_EXIT=7 gh api repos/o/r/pulls/1 --jq .number <<<"тело-запроса" 2>"$WORK/shim.err") && shim_rc=0 || shim_rc=$?
if [ "$direct_rc" != "7" ] || [ "$shim_rc" != "7" ]; then
  note "FAIL случай 2: exit code не 7 (direct=$direct_rc shim=$shim_rc) — тест сам сломан"; fail=1
elif [ "$direct_out" != "$shim_out" ]; then
  note "FAIL случай 2: stdout разошёлся (direct=[$direct_out] shim=[$shim_out])"; fail=1
elif ! diff -q "$WORK/direct.err" "$WORK/shim.err" >/dev/null; then
  note "FAIL случай 2: stderr разошёлся"; diff "$WORK/direct.err" "$WORK/shim.err" || true; fail=1
elif [ "$shim_rc" != "$direct_rc" ]; then
  note "FAIL случай 2: код возврата разошёлся (direct=$direct_rc shim=$shim_rc)"; fail=1
elif ! grep -qF "API-STDIN:тело-запроса" <<<"$shim_out"; then
  note "FAIL случай 2: stdin не дошёл через шим (potokovый ввод, --input -)"; fail=1
else
  note "OK случай 2: gh api — argv/stdout/stderr/exit code/stdin идентичны напрямую вызванному gh"
fi

# ── случай 3: gh pr view / gh issue list / gh run list — тот же код возврата ─
# `cmd1=$(cmd2)` под set -e прерывает скрипт на ненулевом коде ДО следующей
# строки (это простая команда, не защищённая if/&&/||) — идиома
# `... && rc=0 || rc=$?` обязательна для каждого вызова с ожидаемым ненулевым
# кодом возврата (случай gh run list ниже, REALGH_EXIT=3).
d3=$(REALGH_EXIT=0 "$WORK/real-bin/gh" pr view 42 --json number 2>&1) && rc_d3=0 || rc_d3=$?
s3=$(REALGH_EXIT=0 gh pr view 42 --json number 2>&1) && rc_s3=0 || rc_s3=$?
d4=$("$WORK/real-bin/gh" issue list --label task 2>&1) && rc_d4=0 || rc_d4=$?
s4=$(gh issue list --label task 2>&1) && rc_s4=0 || rc_s4=$?
d5=$(REALGH_EXIT=3 "$WORK/real-bin/gh" run list --workflow=worker.yml 2>&1) && rc_d5=0 || rc_d5=$?
s5=$(REALGH_EXIT=3 gh run list --workflow=worker.yml 2>&1) && rc_s5=0 || rc_s5=$?
if [ "$d3" != "$s3" ] || [ "$rc_d3" != "$rc_s3" ]; then
  note "FAIL случай 3a: gh pr view разошёлся (direct=[$d3]/$rc_d3 shim=[$s3]/$rc_s3)"; fail=1
elif [ "$d4" != "$s4" ] || [ "$rc_d4" != "$rc_s4" ]; then
  note "FAIL случай 3b: gh issue list разошёлся (direct=[$d4]/$rc_d4 shim=[$s4]/$rc_s4)"; fail=1
elif [ "$d5" != "$s5" ] || [ "$rc_d5" != "$rc_s5" ]; then
  note "FAIL случай 3c: gh run list разошёлся (direct=[$d5]/$rc_d5 shim=[$s5]/$rc_s5)"; fail=1
else
  note "OK случай 3: gh pr view / gh issue list / gh run list идентичны напрямую вызванному gh (включая ненулевой код возврата)"
fi

# ── случай 4: без рекурсии — шим не вызывает сам себя ────────────────────────
# Если бы шим искал "gh" заново через command -v после того, как сам встал в
# PATH первым, он нашёл бы себя же — бесконечная рекурсия. Живой признак:
# случаи 1-3 выше уже завершились (без зависания/переполнения стека) — здесь
# явный ассерт: GH_SHIM_REAL_GH обязан указывать на файл ВНЕ каталога шима.
case "$GH_SHIM_REAL_GH" in
  "$WORK/gh-shim/"*)
    note "FAIL случай 4: GH_SHIM_REAL_GH указывает внутрь каталога шима — риск рекурсии: $GH_SHIM_REAL_GH"; fail=1 ;;
  *)
    note "OK случай 4: GH_SHIM_REAL_GH ($GH_SHIM_REAL_GH) вне каталога шима — рекурсии нет" ;;
esac

# ── случай 5 (мутация): шим снят из PATH — тот же вызов обязан достучаться
# до настоящего gh. Доказывает, что случай 1 был красным по вине ИМЕННО
# присутствия шима в PATH, а не какого-то стороннего механизма.
rm -f "$MARKER"
export PATH="$BASE_PATH"
if ! out=$(gh pr create --title t --body-file "$WORK/body.md" 2>"$WORK/err5"); then
  note "FAIL случай 5: без шима в PATH gh pr create ВСЁ РАВНО отклонён — случай 1 не доказывает работу шима"; cat "$WORK/err5"; fail=1
elif [ ! -f "$MARKER" ]; then
  note "FAIL случай 5: без шима в PATH настоящий gh не был вызван — случай 1 недостоверен"; fail=1
else
  note "OK случай 5 (мутация): без шима в PATH тот же вызов доходит до настоящего gh — случай 1 доказан честно"
fi

# ── случай 6: сломанная установка (нет GH_SHIM_REAL_GH) — громкий отказ, ────
# не тихая деградация. Прогон САМОГО файла шима напрямую (не через PATH —
# здесь мы уже нарочно проверяем именно его встроенный guard).
unset GH_SHIM_REAL_GH || true
if out=$("$WORK/gh-shim/gh" pr view 1 2>"$WORK/err6"); then
  note "FAIL случай 6: шим без GH_SHIM_REAL_GH не упал — тихая деградация: $out"; fail=1
elif ! grep -q "GH_SHIM_REAL_GH" "$WORK/err6"; then
  note "FAIL случай 6: сообщение об ошибке не объясняет причину (сломанная установка)"; cat "$WORK/err6"; fail=1
else
  note "OK случай 6: без GH_SHIM_REAL_GH шим падает громко, а не тихо"
fi

exit "$fail"
