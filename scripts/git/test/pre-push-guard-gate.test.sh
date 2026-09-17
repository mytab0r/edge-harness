#!/usr/bin/env bash
# Обязательный локальный гейт гвардий перед push (issue #1280): доказывает
# поведение РЕАЛЬНЫХ .githooks/pre-push + scripts/lib/pre_push_guard_gate.py
# на живом git-репозитории (не пересказ) — файлы копируются как есть, не
# переписываются здесь заново. Каталог гвардий (scripts/ci/run_guards.sh +
# один управляемый fake-guard) вынесен ВНЕ пушащего репозитория и подключён
# через GUARD_GATE_REPO_ROOT — боевые 82 гвардии каталога сюда не тянутся
# (минуты, сеть, ловушка #1228), тестируется сам МЕХАНИЗМ гейта.
#
# Случаи:
#   1) каталог гвардий зелёный                       — push проходит;
#   2) каталог гвардий красный                        — push отклонён,
#      сообщение называет причину и путь исправления;
#   3) каталог гвардий красный + GUARD_GATE_SKIP_ACK   — push проходит с
#      предупреждением (аварийный выход, issue #1280, п. 5);
#   4) run_guards.sh отсутствует (третье состояние)    — push отклонён с
#      текстом «НЕ СМОГ запуститься», отличным от текста нарушения;
#   5) gh CLI недоступен (находка ai-ревью PR #1285)   — push отклонён
#      ТРЕТЬИМ состоянием («окружение не даёт достоверный прогон»), не
#      красной гвардией: красные без gh (7 из 82 каталога) — слепота
#      машины, не код.
#
# Мутация, которой доказана гвардия (issue #1280): закомментируй строку
# `exec "$python_bin" ...` в .githooks/pre-push (замени на `exit 0`) —
# случай (2) перестаёт отклоняться, тест красный. Верни строку — снова зелёный.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
HOOK_SRC="$REPO_ROOT/.githooks/pre-push"
GATE_SRC="$REPO_ROOT/scripts/lib/pre_push_guard_gate.py"
CHECK_RESULT_SRC="$REPO_ROOT/scripts/lib/check_result.py"
CONSOLE_UTF8_SRC="$REPO_ROOT/scripts/lib/console_utf8.py"
RUN_GUARDS_SRC="$REPO_ROOT/scripts/ci/run_guards.sh"

WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

fail=0
note() { echo "$@"; }

# ── синтетический каталог гвардий, вне пушащего репозитория ──
CATALOG="$WORK/catalog"
mkdir -p "$CATALOG/scripts/ci/guards"
cp "$RUN_GUARDS_SRC" "$CATALOG/scripts/ci/run_guards.sh"
cat >"$CATALOG/scripts/ci/guards/fake-guard.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
state_file="${FAKE_GUARD_STATE_FILE:?}"
state="$(cat "$state_file")"
if [ "$state" = "red" ]; then
  echo "fake-guard: нарушение (тестовая красная гвардия)" >&2
  exit 1
fi
echo "fake-guard: чисто"
EOF
chmod +x "$CATALOG/scripts/ci/guards/fake-guard.sh"

STATE_FILE="$WORK/fake-guard-state"

# ── bare origin + push-репозиторий с прод-файлами хука и гейта ──
git init -q --bare "$WORK/origin.git"

git init -q "$WORK/pusher"
(
  cd "$WORK/pusher"
  git config user.email test@example.com
  git config user.name test
  mkdir -p .githooks scripts/lib
  cp "$HOOK_SRC" .githooks/pre-push
  chmod +x .githooks/pre-push
  cp "$GATE_SRC" scripts/lib/pre_push_guard_gate.py
  cp "$CHECK_RESULT_SRC" scripts/lib/check_result.py
  cp "$CONSOLE_UTF8_SRC" scripts/lib/console_utf8.py
  git add .githooks scripts
  git commit -q -m init
  git remote add origin "$WORK/origin.git"
  git config core.hooksPath .githooks
)

try_push() {
  # env -u GITHUB_ACTIONS: доказываем local-режим гейта явно, не полагаясь
  # на окружение раннера, в котором выполняется сам этот тест (тот же класс
  # осторожности, что в worktree-guard.test.sh).
  (
    cd "$WORK/pusher"
    echo "change at $(date +%s%N)" >>note.txt
    git add note.txt
    git commit -q -m "test commit"
    env -u GITHUB_ACTIONS GUARD_GATE_REPO_ROOT="$CATALOG" FAKE_GUARD_STATE_FILE="$STATE_FILE" \
      git push -q origin HEAD:refs/heads/main
  )
}

# ── случай 1: зелёная гвардия — push проходит ──
echo green >"$STATE_FILE"
if out="$(try_push 2>&1)"; then
  note "случай 1 (зелёная гвардия): push прошёл — ОК"
else
  note "случай 1 (зелёная гвардия): push ОТКЛОНЁН — ОШИБКА, ожидался успех"
  echo "$out" >&2
  fail=1
fi

# ── случай 2: красная гвардия — push отклонён с адресным сообщением ──
echo red >"$STATE_FILE"
if out="$(try_push 2>&1)"; then
  note "случай 2 (красная гвардия): push прошёл — ОШИБКА, ожидался отказ"
  fail=1
else
  note "случай 2 (красная гвардия): push отклонён — ОК"
  case "$out" in
    *"guard-gate"*"run_guards.sh завершился с кодом"*)
      note "  сообщение называет причину: $(printf '%s' "$out" | grep -m1 'guard-gate')" ;;
    *)
      note "  ОШИБКА: сообщение не называет причину отказа (не адресное)"
      echo "$out" >&2
      fail=1
      ;;
  esac
fi

# ── случай 3: красная гвардия + аварийный выход — push проходит с предупреждением ──
echo red >"$STATE_FILE"
if (
  cd "$WORK/pusher"
  echo "change at $(date +%s%N)" >>note.txt
  git add note.txt
  git commit -q -m "test commit (ack)"
  env -u GITHUB_ACTIONS GUARD_GATE_REPO_ROOT="$CATALOG" FAKE_GUARD_STATE_FILE="$STATE_FILE" \
    GUARD_GATE_SKIP_ACK="тест: аварийный выход" \
    git push -q origin HEAD:refs/heads/main 2>"$WORK/ack.stderr"
); then
  if grep -q "GUARD_GATE_SKIP_ACK" "$WORK/ack.stderr"; then
    note "случай 3 (аварийный выход): push прошёл с видимым предупреждением — ОК"
  else
    note "случай 3 (аварийный выход): предупреждение не найдено в выводе — ОШИБКА"
    cat "$WORK/ack.stderr" >&2
    fail=1
  fi
else
  note "случай 3 (аварийный выход): push ОТКЛОНЁН — ОШИБКА, аварийный выход обязан пропускать"
  cat "$WORK/ack.stderr" >&2
  fail=1
fi

# ── случай 4: run_guards.sh отсутствует — третье состояние, не «прошло» ──
EMPTY_CATALOG="$WORK/empty-catalog"
mkdir -p "$EMPTY_CATALOG"
(
  cd "$WORK/pusher"
  echo "change at $(date +%s%N)" >>note.txt
  git add note.txt
  git commit -q -m "test commit (unknown)"
  if out="$(env -u GITHUB_ACTIONS GUARD_GATE_REPO_ROOT="$EMPTY_CATALOG" \
    git push -q origin HEAD:refs/heads/main 2>&1)"; then
    note "случай 4 (гейт не может запуститься): push прошёл — ОШИБКА, третье состояние обязано блокировать"
    fail=1
  else
    case "$out" in
      *"НЕ СМОГ запуститься"*)
        note "случай 4 (гейт не может запуститься): push отклонён с текстом третьего состояния — ОК" ;;
      *)
        note "случай 4 (гейт не может запуститься): текст отказа не называет третье состояние — ОШИБКА"
        echo "$out" >&2
        fail=1
        ;;
    esac
  fi
)

# ── случай 5: gh CLI недоступен — окружение не даёт достоверный прогон ──
# PATH заменяется на каталог-шейм со всеми исполняемыми текущего PATH,
# КРОМЕ gh*: гейт обязан вернуть ТРЕТЬЕ состояние (факт про gh), не
# красную гвардию, и заблокировать пуш.
NOGH_BIN="$WORK/bin-without-gh"
mkdir -p "$NOGH_BIN"
no_gh_ifs="$IFS"
IFS=":"
for dir in $PATH; do
  [ -d "$dir" ] || continue
  for exe in "$dir"/*; do
    [ -e "$exe" ] && [ -x "$exe" ] || continue
    base="$(basename "$exe")"
    case "$base" in gh|gh.*) continue ;; esac
    [ -e "$NOGH_BIN/$base" ] || ln -s "$exe" "$NOGH_BIN/$base" 2>/dev/null || true
  done
done
IFS="$no_gh_ifs"
(
  cd "$WORK/pusher"
  echo "change at $(date +%s%N)" >>note.txt
  git add note.txt
  git commit -q -m "test commit (gh-missing)"
  if out="$(env -u GITHUB_ACTIONS PATH="$NOGH_BIN" GUARD_GATE_REPO_ROOT="$CATALOG" \
    git push -q origin HEAD:refs/heads/main 2>&1)"; then
    note "случай 5 (gh недоступен): push прошёл — ОШИБКА, недостоверный прогон обязан блокировать"
    fail=1
  else
    if printf '%s' "$out" | grep -q "НЕ СМОГ запуститься" \
      && printf '%s' "$out" | grep -q "gh CLI не найден"; then
      note "случай 5 (gh недоступен): push отклонён третьим состоянием с фактом про gh — ОК"
    else
      note "случай 5 (gh недоступен): отказ не называет ни третье состояние, ни gh — ОШИБКА"
      echo "$out" >&2
      fail=1
    fi
  fi
)

if [ "$fail" = 0 ]; then
  echo "pre-push-guard-gate: все пять случаев прошли как ожидалось"
fi
exit "$fail"
