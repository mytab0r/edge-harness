#!/usr/bin/env bash
# Проверка на входе scripts/gh/issue-create (#526, #720): issue без метки `task`
# ИЛИ без машиночитаемого объявления связи отклоняется ДО вызова
# `gh issue create` — реальный `gh` не вызывается вовсе (фейковый gh ниже
# оставляет маркер только при настоящем вызове `issue create`, тест проверяет
# это явно, а не по коду возврата).
#
# Живой случай, который эта обёртка закрывает: issue #523 (`gh issue create
# --label white-spot`, без task — пул воркера её не увидел) и #425 (без
# единой метки).
#
# Мутация, которой доказана проверка метки task: закомментируй блок `if [ "$has_task"
# -ne 1 ]` в scripts/gh/issue-create (до `fi` перед `exec gh issue create`) —
# случай 1 (--label white-spot без task) перестаёт отклоняться и оставляет
# маркер, тест краснеет. Верни блок — тест снова зелёный.
#
# Мутация, которой доказана проверка объявления связи (#720): закомментируй
# блок `if ! printf '%s' "$body" | python3 ... pool_issue.py check-body`
# (весь `if [ "$has_task" -eq 1 ]` перед проверкой приоритета) — случаи 3-8
# и 11-15 перестают вести себя по спецификации: тело без связи перестаёт
# отклоняться (случаи 3 и 14 краснеют маркером вызова gh), тест краснеет.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SCRIPT_SRC="$REPO_ROOT/scripts/gh/issue-create"

WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

mkdir -p "$WORK/bin"
MARKER="$WORK/gh-issue-create-called"
cat >"$WORK/bin/gh" <<GHEOF
#!/usr/bin/env bash
if [ "\$1" = "repo" ] && [ "\$2" = "view" ]; then
  echo "o/r"
  exit 0
fi
if [ "\$1" = "issue" ] && [ "\$2" = "create" ]; then
  echo called >"$MARKER"
  echo "https://github.com/o/r/issues/1"
  exit 0
fi
echo "unexpected gh call: \$*" >&2
exit 1
GHEOF
chmod +x "$WORK/bin/gh"
export PATH="$WORK/bin:$PATH"

# Приоритетная печать (#695) — сеть подменена фикстурой (пустой пул), этот
# файл тестирует ТОЛЬКО метку task и объявление связи, не приоритет (тот —
# issue-create-priority-gate.test.sh) и не дедуп (тот —
# issue-create-duplicate-guard.test.sh).
export PRIORITY_TOP_FIXTURE="$WORK/priority-fixture.json"
echo '[]' >"$PRIORITY_TOP_FIXTURE"
# Дедуп — пустой пул
export DUPLICATE_GUARD_FIXTURE="$WORK/dup-fixture.json"
echo '[]' >"$DUPLICATE_GUARD_FIXTURE"
# Явный --not-process-ack, чтобы не упереться в приоритетный тормоз (#695)
NOT_PROCESS_ACK=(--not-process-ack "не про приоритет, тест метки task и связи (#526/#720)")

fail=0
note() { echo "$@"; }

# ── случай 1: только white-spot, без task — отказ, gh не вызван ──────────────
rm -f "$MARKER"
if bash "$SCRIPT_SRC" --title t --body b --label white-spot >"$WORK/out1" 2>"$WORK/err1"; then
  note "FAIL случай 1: issue-create принял --label white-spot без task"; fail=1
elif [ -f "$MARKER" ]; then
  note "FAIL случай 1: gh issue create был вызван, хотя task отсутствует"; fail=1
elif ! grep -q "без метки task" "$WORK/err1"; then
  note "FAIL случай 1: нет объяснения в stderr"; cat "$WORK/err1"; fail=1
else
  note "OK случай 1: --label white-spot без task отклонён, gh не вызван"
fi

# ── случай 2: вовсе без --label (класс #425) — отказ ──────────────────────────
rm -f "$MARKER"
if bash "$SCRIPT_SRC" --title t --body b >"$WORK/out2" 2>"$WORK/err2"; then
  note "FAIL случай 2: issue-create принял issue вовсе без меток"; fail=1
elif [ -f "$MARKER" ]; then
  note "FAIL случай 2: gh вызван без единой метки"; fail=1
else
  note "OK случай 2: issue без меток отклонена (класс #425)"
fi

# ── случай 3: --label task, БЕЗ объявления связи — отказ (#720) ──────────────
rm -f "$MARKER"
if bash "$SCRIPT_SRC" --title t --body "просто тело без связи" --label task "${NOT_PROCESS_ACK[@]}" >"$WORK/out3" 2>"$WORK/err3"; then
  note "FAIL случай 3: issue-create принял task без объявления связи"; fail=1
elif [ -f "$MARKER" ]; then
  note "FAIL случай 3: gh issue create был вызван, хотя объявления связи нет"; fail=1
elif ! grep -q "машиночитаемого объявления связи" "$WORK/err3"; then
  note "FAIL случай 3: нет объяснения про объявление связи в stderr"; cat "$WORK/err3"; fail=1
elif ! grep -q "Чем блокируется" "$WORK/err3"; then
  note "FAIL случай 3: нет готовой строки для вставки в stderr"; cat "$WORK/err3"; fail=1
else
  note "OK случай 3: --label task без объявления связи отклонён, gh не вызван"
fi

# ── случай 4: --label task + структурное поле "ничем" — принимает ────────────
rm -f "$MARKER"
body4="### Чем блокируется
ничем
"
if ! bash "$SCRIPT_SRC" --title t --body "$body4" --label task "${NOT_PROCESS_ACK[@]}" >"$WORK/out4" 2>"$WORK/err4"; then
  note "FAIL случай 4: структурное поле 'ничем' отклонено"; cat "$WORK/err4"; fail=1
elif [ ! -f "$MARKER" ]; then
  note "FAIL случай 4: gh issue create не вызван для структурного поля 'ничем'"; fail=1
else
  note "OK случай 4: структурное поле 'ничем' принято, gh вызван"
fi

# ── случай 5: --label task + инлайн 'БЛОКИРУЕТСЯ: ничем' — принимает ──────────
rm -f "$MARKER"
body5="Тело задачи
БЛОКИРУЕТСЯ: ничем"
if ! bash "$SCRIPT_SRC" --title t --body "$body5" --label task "${NOT_PROCESS_ACK[@]}" >"$WORK/out5" 2>"$WORK/err5"; then
  note "FAIL случай 5: инлайн 'ничем' отклонен"; cat "$WORK/err5"; fail=1
elif [ ! -f "$MARKER" ]; then
  note "FAIL случай 5: gh не вызван для инлайн 'ничем'"; fail=1
else
  note "OK случай 5: инлайн 'БЛОКИРУЕТСЯ: ничем' принят, gh вызван"
fi

# ── случай 6: --label task + структурное поле с номерами — принимает ─────────
rm -f "$MARKER"
body6="### Чем блокируется
#123 #456
"
if ! bash "$SCRIPT_SRC" --title t --body "$body6" --label task "${NOT_PROCESS_ACK[@]}" >"$WORK/out6" 2>"$WORK/err6"; then
  note "FAIL случай 6: структурное поле с номерами отклонено"; cat "$WORK/err6"; fail=1
elif [ ! -f "$MARKER" ]; then
  note "FAIL случай 6: gh не вызван для структурного поля с номерами"; fail=1
else
  note "OK случай 6: структурное поле с номерами принято, gh вызван"
fi

# ── случай 7: --label task + инлайн с номерами — принимает ───────────────────
rm -f "$MARKER"
body7="Тело задачи
БЛОКИРУЕТСЯ: #123 #456"
if ! bash "$SCRIPT_SRC" --title t --body "$body7" --label task "${NOT_PROCESS_ACK[@]}" >"$WORK/out7" 2>"$WORK/err7"; then
  note "FAIL случай 7: инлайн с номерами отклонен"; cat "$WORK/err7"; fail=1
elif [ ! -f "$MARKER" ]; then
  note "FAIL случай 7: gh не вызван для инлайна с номерами"; fail=1
else
  note "OK случай 7: инлайн 'БЛОКИРУЕТСЯ: #123 #456' принят, gh вызван"
fi

# ── случай 8: --label task,white-spot одним значением — принимает (при наличии связи) ─
rm -f "$MARKER"
body8="### Чем блокируется
ничем
"
if ! bash "$SCRIPT_SRC" --title t --body "$body8" --label "task,white-spot" "${NOT_PROCESS_ACK[@]}" >"$WORK/out8" 2>"$WORK/err8"; then
  note "FAIL случай 8: комбинированный --label с task отклонён"; cat "$WORK/err8"; fail=1
elif [ ! -f "$MARKER" ]; then
  note "FAIL случай 8: gh не вызван для комбинированного --label с task"; fail=1
else
  note "OK случай 8: 'task,white-spot' в одном --label распознан как содержащий task"
fi

# ── случай 9: white-spot,area:process без task одним значением — отказ ───────
rm -f "$MARKER"
if bash "$SCRIPT_SRC" --title t --body b --label "white-spot,area:process" >"$WORK/out9" 2>"$WORK/err9"; then
  note "FAIL случай 9: комбинированный --label без task принят"; fail=1
elif [ -f "$MARKER" ]; then
  note "FAIL случай 9: gh вызван для комбинированного --label без task"; fail=1
else
  note "OK случай 9: 'white-spot,area:process' без task отклонён"
fi

# ── случай 10: --no-task-ack "причина" — осознанный пропуск проверки task, но связь НЕ проверяется ──────
# (--no-task-ack объявляет "это не задача пула", проверка связи только для пула)
rm -f "$MARKER"
if ! bash "$SCRIPT_SRC" --title t --body b --label white-spot --no-task-ack "не задача пула" >"$WORK/out10" 2>"$WORK/err10"; then
  note "FAIL случай 10: --no-task-ack отклонён"; cat "$WORK/err10"; fail=1
elif [ ! -f "$MARKER" ]; then
  note "FAIL случай 10: gh не вызван при явном --no-task-ack"; fail=1
elif ! grep -q "не задача пула" "$WORK/err10"; then
  note "FAIL случай 10: причина --no-task-ack не попала в лог"; cat "$WORK/err10"; fail=1
else
  note "OK случай 10: --no-task-ack осознанно пропускает проверку task, gh вызван"
fi

# ── случай 11: разные уровни заголовка (##, ###, ####) — все валидны ──────────
for header in "## Чем блокируется" "### Чем блокируется" "#### Чем блокируется"; do
  rm -f "$MARKER"
  body11="$header
ничем
"
  if ! bash "$SCRIPT_SRC" --title t --body "$body11" --label task "${NOT_PROCESS_ACK[@]}" >"$WORK/out11" 2>"$WORK/err11"; then
    note "FAIL случай 11: заголовок '$header' отклонен"; cat "$WORK/err11"; fail=1
  elif [ ! -f "$MARKER" ]; then
    note "FAIL случай 11: gh не вызван для заголовка '$header'"; fail=1
  else
    note "OK случай 11: заголовок '$header' принят"
  fi
done

# ── случай 12: пустой ответ после заголовка — валидно ────────────────────────
rm -f "$MARKER"
body12="### Чем блокируется

"
if ! bash "$SCRIPT_SRC" --title t --body "$body12" --label task "${NOT_PROCESS_ACK[@]}" >"$WORK/out12" 2>"$WORK/err12"; then
  note "FAIL случай 12: пустой ответ после заголовка отклонен"; cat "$WORK/err12"; fail=1
elif [ ! -f "$MARKER" ]; then
  note "FAIL случай 12: gh не вызван для пустого ответа"; fail=1
else
  note "OK случай 12: пустой ответ после заголовка принят"
fi

# ── случай 13: ответ из одних пробелов между заголовком и «ничем» — валидно ──
# Живой случай — находка AI-ревью PR #804: awk-копия проверки считала строку
# из пробелов ответом-прозой и отказывала тело, которое Python-гейт и
# declared_deps (место правды) принимали. Парсер теперь один (check-body из
# pool_issue.py) — обёртка обязана соглашаться с читателем графа на этой же
# форме. Мутация: верни в scripts/gh/issue-create локальную копию разбора
# (awk/grep) вместо делегации pool_issue.py check-body — любое расхождение
# с Python-гейтом снова станет видимым на этом случае и случае 12.
rm -f "$MARKER"
# Строка из ОДНОГО пробела между заголовком и ответом — собрана printf'ом,
# чтобы авто-обрезка хвостовых пробелов в редакторе не свела случай к 12-му.
body13="$(printf '### Чем блокируется\n \nничем\n')"
if ! bash "$SCRIPT_SRC" --title t --body "$body13" --label task "${NOT_PROCESS_ACK[@]}" >"$WORK/out13" 2>"$WORK/err13"; then
  note "FAIL случай 13: ответ из одних пробелов отклонен"; cat "$WORK/err13"; fail=1
elif [ ! -f "$MARKER" ]; then
  note "FAIL случай 13: gh не вызван для ответа из одних пробелов"; fail=1
else
  note "OK случай 13: ответ из одних пробелов перед 'ничем' принят"
fi

# ── случай 14: пустое тело — отказ ДО gh issue create ────────────────────────
# Объявления в пустом теле нет; первый заход PR #804 пропускал его молча.
rm -f "$MARKER"
if bash "$SCRIPT_SRC" --title t --label task "${NOT_PROCESS_ACK[@]}" >"$WORK/out14" 2>"$WORK/err14"; then
  note "FAIL случай 14: issue-create принял task с пустым телом"; fail=1
elif [ -f "$MARKER" ]; then
  note "FAIL случай 14: gh issue create был вызван для пустого тела"; fail=1
elif ! grep -q "машиночитаемого объявления связи" "$WORK/err14"; then
  note "FAIL случай 14: нет объяснения про объявление связи в stderr"; cat "$WORK/err14"; fail=1
else
  note "OK случай 14: пустое тело отклонено, gh не вызван"
fi

# ── случай 15: голый номер без # в ответе поля — валидно ─────────────────────
# То же, что читает auto_wire (declared_deps._VALUE_NUMBER_RE берёт #?\d{2,5}):
# гейт, требующий '#', отвергал бы тело, которое граф потом разберёт.
rm -f "$MARKER"
body15="### Чем блокируется
55
"
if ! bash "$SCRIPT_SRC" --title t --body "$body15" --label task "${NOT_PROCESS_ACK[@]}" >"$WORK/out15" 2>"$WORK/err15"; then
  note "FAIL случай 15: голый номер в ответе поля отклонен"; cat "$WORK/err15"; fail=1
elif [ ! -f "$MARKER" ]; then
  note "FAIL случай 15: gh не вызван для голого номера"; fail=1
else
  note "OK случай 15: голый номер без # в ответе поля принят"
fi

exit "$fail"
