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
#
# Остальные мутации:
# — случай 5a («дверь под шимом»): верни в scripts/git/pr-create голый
#   `exec gh pr create` (без GH_SHIM_REAL_GH) — дверь под шимом отклонит сама
#   себя (петля «шим отсылает к двери, дверь отклоняется тем же шимом», живая
#   находка ревью PR #596), настоящий gh вызван не будет — тест красный.
# — случай 7 (source-гвардия проводки): выкини из scripts/worker/task.sh
#   source gh_shim.sh, вызов gh_shim_install или `|| die` при нём — тест
#   красный. Проводка шима в транспорт закреплена тестом, а не комментарием:
#   без этого защита исчезает молча при рефакторинге task.sh.
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

# ── случай 5a: дверь scripts/git/pr-create ПОД шимом доходит до настоящего gh ──
# Шим отклоняет голый `gh pr create`; дверь — единственный штатный путь —
# обязана работать в ТОЙ ЖЕ среде: резолвит настоящий gh через
# GH_SHIM_REAL_GH (та же переменная, одно место правды), а не голое слово
# `gh`, которое PATH вернул бы обратно в шим. Без этого ассерта видимый
# результат задачи — «обход невозможен И дверь работает» — держался бы на
# рассуждении, а не на тесте (случай 1 дверь не прогоняет).
if ! gh_shim_install "$WORK/gh-shim" >"$WORK/install5a.log" 2>&1; then
  note "FAIL случай 5a: повторная установка шима не удалась"; cat "$WORK/install5a.log"; fail=1
fi
cat >"$WORK/body-door.md" <<'DOORBODY'
#594

## Что сделано
PATH-шим gh в среде воркера: голый gh pr create отклоняется, остальное прозрачно.
DOORBODY
rm -f "$MARKER"
if ! door_out=$("$REPO_ROOT/scripts/git/pr-create" --base main --head agent/594-gh-pr-create-path-shim --title "дверь под шимом" --body-file "$WORK/body-door.md" 2>"$WORK/err5a"); then
  note "FAIL случай 5a: дверь под шимом не сработала (петля «шим → дверь → шим»?)"; cat "$WORK/err5a"; fail=1
elif [ ! -f "$MARKER" ]; then
  note "FAIL случай 5a: дверь под шимом НЕ дошла до настоящего gh:"; cat "$WORK/err5a"; fail=1
elif ! grep -q "https://github.test/o/r/pull/1" <<<"$door_out"; then
  note "FAIL случай 5a: в stdout двери нет URL от настоящего gh: [$door_out]"; fail=1
else
  note "OK случай 5a: дверь под шимом доходит до настоящего gh — обход невозможен И дверь работает"
fi

# ── случай 5b: та же дверь под шимом держит свою гвардию тела ДО любого gh ───
# Дверь — вход защиты: тело с Closes/Fixes/Resolves отклоняется ею самой до
# сетевого вызова, даже когда шим уже стоит в PATH (тогда PR не создаётся
# дважды: ни шимом, ни дверью).
printf '#594\n\nCloses #594.\n' >"$WORK/body-door-closes.md"
rm -f "$MARKER"
if out5b=$("$REPO_ROOT/scripts/git/pr-create" --base main --head agent/594-gh-pr-create-path-shim --title "тест" --body-file "$WORK/body-door-closes.md" 2>"$WORK/err5b"); then
  note "FAIL случай 5b: дверь пропустила тело с Closes #N: $out5b"; fail=1
elif [ -f "$MARKER" ]; then
  note "FAIL случай 5b: тело с Closes #N дошло до настоящего gh — гвардия тела двери не сработала"; fail=1
elif ! grep -q "PR НЕ создан" "$WORK/err5b"; then
  note "FAIL случай 5b: в stderr нет «PR НЕ создан»"; cat "$WORK/err5b"; fail=1
else
  note "OK случай 5b: дверь под шимом отклоняет Closes #N до сети — ничего не создаётся"
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

# ── случай 7 (source-гвардия): проводка шима в scripts/worker/task.sh ────────
# закреплена тестом, а не комментарием (находка ревью PR #596 — смок раньше
# звал gh_shim_install сам и в task.sh не заглядывал). Четыре инварианта: (7a)
# task.sh source'ит scripts/lib/gh_shim.sh; (7b) вызывает gh_shim_install;
# (7c) вызов стоит ДО запуска DSH (первый реальный запуск агента) — иначе DSH
# стартует без шима и голый gh pr create вновь физически достижим; (7d) при
# неудаче установки — `die`, а не тихое продолжение (fail loud). Выкини любое
# из четырёх — тест красный. Комментарии кода не учитываются: гвардия смотрит
# на исполняемые строки, правка комментария её не красит и не зеленит.
#
# Имя точки входа DSH менялось (dsh_run_with_retry → dsh_run_with_pool_then_chain,
# #727/#737/#877/#880) — гвардия ищет по alternation нескольких известных имён,
# а не одно жёстко зашитое, чтобы следующее переименование не роняло тест
# молча мимо цели (класс, пойманный ребейзом PR #596 на #877/#880: старое имя
# исчезло из исполняемых строк, дистанцию мерить стало не с чем).
DSH_ENTRYPOINT_RE='dsh_run_with_pool_then_chain|dsh_run_with_provider_chain|dsh_run_with_retry'
TASK_SH="$REPO_ROOT/scripts/worker/task.sh"
code7="$(grep -v '^[[:space:]]*#\|^[[:space:]]*$' "$TASK_SH")" || code7=""
# `|| true`/`|| var=` обязательны на каждом grep: скрипт под set -euo pipefail,
# grep без совпадения (ровно случай «вызов выкинули из task.sh») иначе убил бы
# тест МОЛЧА, без строки FAIL — а красный тест обязан называть причину.
install_inv="$(grep 'gh_shim_install' <<<"$code7" | head -1)" || install_inv=""
if ! grep -q 'lib/gh_shim.sh' <<<"$code7"; then
  note "FAIL случай 7a: task.sh больше не source'ит scripts/lib/gh_shim.sh — шим не ставится, обход вновь возможен"; fail=1
elif [ -z "$install_inv" ]; then
  note "FAIL случай 7b: из task.sh пропал вызов gh_shim_install — шим не ставится, обход вновь возможен"; fail=1
else
  install_line="$(grep -n 'gh_shim_install' <<<"$code7" | head -1 | cut -d: -f1)" || install_line=""
  dsh_line="$(grep -nE "$DSH_ENTRYPOINT_RE" <<<"$code7" | head -1 | cut -d: -f1)" || dsh_line=""
  if [ -z "$dsh_line" ]; then
    note "FAIL случай 7c: в task.sh не найден ни один известный вызов точки входа DSH ($DSH_ENTRYPOINT_RE) — гвардия порядка сломана, обнови alternation на актуальное имя (это дефект теста, а не транспорта)"; fail=1
  elif [ "$install_line" -ge "$dsh_line" ]; then
    note "FAIL случай 7c: gh_shim_install (строка $install_line) стоит НЕ раньше запуска DSH (строка $dsh_line) — DSH стартует без шима"; fail=1
  elif ! grep -q 'die' <<<"$install_inv"; then
    note "FAIL случай 7d: вызов gh_shim_install без громкого отказа (|| die) — сломанная установка деградирует тихо: $install_inv"; fail=1
  else
    note "OK случай 7: task.sh source'ит шим, ставит его до dsh ($install_line < $dsh_line) и падает громко при неудаче"
  fi
fi

exit "$fail"
