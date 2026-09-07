#!/usr/bin/env bash
# Проверка на входе scripts/gh/issue-create (#695): решение о приоритете
# (`area:process`, scripts/lib/free_task.py::META_LABEL) обязано быть явным —
# либо метка area:process среди --label, либо явный --not-process-ack
# "<причина>". Отказывает ДО вызова `gh issue create` — реальный `gh` для
# создания issue не вызывается вовсе, если решение не сделано осознанно.
#
# Живой случай: 2026-09-07 задачи #684/#685/#689 заведены с area:orchestra
# вместо area:process и встали в хвост очереди из 200 задач как обычные
# новые — исправлено вручную постфактум. При этом группа приоритета уже
# несла #194/#168/#226 про тот же дефект другими словами; дедуп по схожести
# заголовка (scripts/lib/duplicate_guard.py, issue-create-duplicate-guard.test.sh)
# их не поймал — сравнивает слова заголовка, а формулировки были разные.
# Второй тормоз этого файла — печать верха группы приоритета ПЕРЕД созданием
# (случай 4 ниже): реальные номера открытых area:process-задач должны быть
# видны в момент заведения, чтобы «та же проблема другими словами» ловилась
# глазами, раз дедуп по заголовку её не ловит и не может (честный потолок
# метода).
#
# Мутация, которой доказана проверка (случай 1): в scripts/gh/issue-create
# закомментируй блок `if [ "$has_meta" -ne 1 ] && [ -z "$not_process_ack" ]`
# целиком (до соответствующего `fi`) — случай 1 (--label task без area:process
# и без --not-process-ack) перестаёт отклоняться и оставляет маркер вызова
# gh issue create, тест краснеет. Верни блок — тест снова зелёный.
set -euo pipefail

# PYTHONUTF8 — python3 в scripts/lib/*.py пишет кириллицу в stderr; на Windows
# консольная кодовая страница (напр. 866) иначе коверкает байты ДО того, как
# они попадут в файл, и случай 5 ниже (сверка текста предупреждения) читает
# мусор вместо сообщения. Тот же флаг, что предписан для pytest в этом репо.
export PYTHONUTF8=1

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SCRIPT_SRC="$REPO_ROOT/scripts/gh/issue-create"

WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

mkdir -p "$WORK/bin"
MARKER="$WORK/gh-issue-create-called"

REPO_VIEW_OUTPUT="$WORK/repo-view-output"
echo "o/r" >"$REPO_VIEW_OUTPUT"

cat >"$WORK/bin/gh" <<GHEOF
#!/usr/bin/env bash
if [ "\$1" = "repo" ] && [ "\$2" = "view" ]; then
  cat "$REPO_VIEW_OUTPUT"
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

# Дедуп по заголовку (#566) — не предмет ЭТОГО файла, пул открытых task
# оставляем пустым, чтобы не упереться во второй, не проверяемый здесь тормоз.
export DUPLICATE_GUARD_FIXTURE="$WORK/dup-fixture.json"
echo '[]' >"$DUPLICATE_GUARD_FIXTURE"

# Верх группы приоритета (#695, случай 4 ниже проверяет реальное содержимое;
# случаи 1-3 используют его же, чтобы не бить по сети).
TOP_FIXTURE="$WORK/priority-fixture.json"
cat >"$TOP_FIXTURE" <<TOPEOF
[
  {"number": 194, "title": "Сторож пропускной способности: конвейер стоит сутки и рапортует о здоровье", "labels": [{"name": "area:process"}], "blocking_open": 2},
  {"number": 168, "title": "Задача с открытым, но нездоровым PR не возвращается в пул", "labels": [{"name": "area:process"}], "blocking_open": 0}
]
TOPEOF
export PRIORITY_TOP_FIXTURE="$TOP_FIXTURE"

fail=0
note() { echo "$@"; }

# ── случай 1: --label task, БЕЗ area:process и БЕЗ --not-process-ack — отказ ──
rm -f "$MARKER"
if bash "$SCRIPT_SRC" --title "Новая задача про узкое место" --body b --label task \
    >"$WORK/out1" 2>"$WORK/err1"; then
  note "FAIL случай 1: issue-create принял задачу без решения о приоритете"; fail=1
elif [ -f "$MARKER" ]; then
  note "FAIL случай 1: gh issue create был вызван, хотя приоритет не решён"; fail=1
elif ! grep -q "area:process" "$WORK/err1"; then
  note "FAIL случай 1: нет упоминания area:process в отказе"; cat "$WORK/err1"; fail=1
elif ! grep -q "not-process-ack" "$WORK/err1"; then
  note "FAIL случай 1: нет упоминания --not-process-ack (какой газ жать) в отказе"; cat "$WORK/err1"; fail=1
else
  note "OK случай 1: задача без явного решения о приоритете отклонена, gh не вызван"
fi

# ── случай 2: --label task,area:process — метка сама решение, gh вызван ──────
rm -f "$MARKER"
if ! bash "$SCRIPT_SRC" --title "Новая задача про узкое место" --body b \
    --label "task,area:process" >"$WORK/out2" 2>"$WORK/err2"; then
  note "FAIL случай 2: --label area:process отклонён"; cat "$WORK/err2"; fail=1
elif [ ! -f "$MARKER" ]; then
  note "FAIL случай 2: gh issue create не вызван при явной метке area:process"; fail=1
else
  note "OK случай 2: метка area:process сама решение — issue создана"
fi

# ── случай 3: --label task + --not-process-ack "причина" — осознанный пропуск ─
rm -f "$MARKER"
if ! bash "$SCRIPT_SRC" --title "Совсем прикладная задача" --body b --label task \
    --not-process-ack "чинит только UI-канарейку, не процесс" >"$WORK/out3" 2>"$WORK/err3"; then
  note "FAIL случай 3: --not-process-ack отклонён"; cat "$WORK/err3"; fail=1
elif [ ! -f "$MARKER" ]; then
  note "FAIL случай 3: gh не вызван при явном --not-process-ack"; fail=1
elif ! grep -q "чинит только UI-канарейку, не процесс" "$WORK/err3"; then
  note "FAIL случай 3: причина --not-process-ack не попала в лог"; cat "$WORK/err3"; fail=1
else
  note "OK случай 3: --not-process-ack осознанно пропускает проверку, причина видна в логе"
fi

# ── случай 4: верх группы приоритета печатается ПЕРЕД созданием ──────────────
rm -f "$MARKER"
if ! bash "$SCRIPT_SRC" --title "Совсем прикладная задача 2" --body b --label task \
    --not-process-ack "тест печати верха приоритета" >"$WORK/out4" 2>"$WORK/err4"; then
  note "FAIL случай 4: вызов отклонён"; cat "$WORK/err4"; fail=1
elif ! grep -q "194" "$WORK/err4"; then
  note "FAIL случай 4: #194 (верх группы приоритета, blocking_open=2) не показан"; cat "$WORK/err4"; fail=1
elif ! grep -q "168" "$WORK/err4"; then
  note "FAIL случай 4: #168 (группа приоритета, blocking_open=0) не показан"; cat "$WORK/err4"; fail=1
elif [ "$(grep -n '194' "$WORK/err4" | head -1 | cut -d: -f1)" -gt "$(grep -n '168' "$WORK/err4" | head -1 | cut -d: -f1)" ]; then
  note "FAIL случай 4: #194 (blocking_open=2) обязан идти раньше #168 (blocking_open=0) — issue_priority_key"; cat "$WORK/err4"; fail=1
else
  note "OK случай 4: верх группы приоритета показан ПЕРЕД созданием, в порядке issue_priority_key"
fi

# ── случай 5: сбой priority-top (не сеть, не пусто) — "пусто" НЕ печатается ──
# Находка ревью PR #697: priority-top при сбое fetch_pool возвращает rc 0 и
# пустой stdout (отличие только в stderr) — до правки обёртка релеила
# предупреждение «верх приоритета не показан» и тут же печатала аффирмативное
# «(пусто — открытых задач нет)» следом, хотя факт не установлен (класс
# «сломано против пусто», docstring free_task.py, находка #247).
# "not-a-repo" (без "/") валит task_deps.fetch_pool на _split_repo
# детерминированно, без сети (тот же приём, что
# test_cli_priority_top_network_failure_warns_but_does_not_block в
# test_free_task.py) — фикстура здесь намеренно снята.
rm -f "$MARKER"
echo "not-a-repo" >"$REPO_VIEW_OUTPUT"
if ! PRIORITY_TOP_FIXTURE= bash "$SCRIPT_SRC" --title "Совсем прикладная задача 3" --body b --label task \
    --not-process-ack "тест сбоя печати верха приоритета" >"$WORK/out5" 2>"$WORK/err5"; then
  note "FAIL случай 5: вызов отклонён при сбое priority-top (это не гейт)"; cat "$WORK/err5"; fail=1
elif [ ! -f "$MARKER" ]; then
  note "FAIL случай 5: gh issue create не вызван при сбое priority-top (это не гейт)"; fail=1
elif ! grep -q "верх приоритета не показан" "$WORK/err5"; then
  note "FAIL случай 5: нет предупреждения о сбое priority-top"; cat "$WORK/err5"; fail=1
elif grep -q "пусто" "$WORK/err5"; then
  note "FAIL случай 5: после сбоя priority-top всё равно напечатано «пусто» — сломано выдано за пусто (находка #697)"; cat "$WORK/err5"; fail=1
else
  note "OK случай 5: сбой priority-top предупреждён, «пусто» НЕ печатается вместо него"
fi
echo "o/r" >"$REPO_VIEW_OUTPUT"

exit "$fail"
