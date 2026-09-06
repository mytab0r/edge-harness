#!/usr/bin/env bash
# Проверка на входе scripts/gh/issue-create (#570, второй слой поверх #566):
# УЛИКА дефекта в ТЕЛЕ новой issue (путь файла/номер прогона Actions/ссылка
# #N/дословная цитата из блока ```) сверяется с ОТКРЫТЫМИ И ЗАКРЫТЫМИ
# задачами пула (scripts/lib/duplicate_guard.py::find_evidence_matches) —
# похожая находится ДО вызова `gh issue create`.
#
# Фикстуры тел — ДОСЛОВНЫЕ тела живых issue (scripts/lib/fixtures_issue_<N>_body.md,
# `gh issue view --json body -q .body`), не пересказ:
#   - #564 vs #562 — измеренный настоящий дубль, ловится ЧЕРЕЗ ДОСЛОВНУЮ
#     ЦИТАТУ ошибки wrangler в блоке ``` (разный перенос строк, тот же текст).
#   - #548 vs #518 — измеренный настоящий случай, который ПЕРВЫЙ слой
#     (заголовок, score 0.18) ЧЕСТНО не ловит, а ВТОРОЙ ловит через прямую
#     ссылку «#518» в теле #548 + общие ссылки #505/#513 (см.
#     scripts/lib/test_duplicate_guard.py за подробным обоснованием).
#   - #121 (реальная, но топически не связанная задача) — отрицательный
#     контроль: не матчится ни с #518, ни с #562.
#
# Мутация, которой доказана проверка: в scripts/gh/issue-create закомментируй
# блок `if [ -n "$matches" ]` целиком (до соответствующего `fi`, оставив
# только `exec gh issue create "${args[@]}"`) — случай 1 (улика без
# --confirm-not-duplicate) перестаёт отклоняться, тест краснеет. Верни блок —
# тест снова зелёный. Дополнительная мутация уровня чистой логики — в
# scripts/lib/test_duplicate_guard.py (докстринг модуля).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SCRIPT_SRC="$REPO_ROOT/scripts/gh/issue-create"

WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

mkdir -p "$WORK/bin"
MARKER="$WORK/gh-issue-create-called"
CREATED_BODY="$WORK/created-body"

# Тела — дословно, читаются из фикстур (не пересказ, не инлайн — см. шапку).
BODY_564="$(cat "$REPO_ROOT/scripts/lib/fixtures_issue_564_body.md")"
BODY_548="$(cat "$REPO_ROOT/scripts/lib/fixtures_issue_548_body.md")"
BODY_UNRELATED="$(cat "$REPO_ROOT/scripts/lib/fixtures_issue_121_body.md")"

# Пул для улик собирается из тех же фикстур python'ом (JSON — надёжнее
# ручного экранирования многострочных тел с ``` и кавычками внутри).
python3 - "$REPO_ROOT" "$WORK" <<'PYEOF'
import json
import sys

repo_root, work = sys.argv[1], sys.argv[2]


def read(number):
    with open(f"{repo_root}/scripts/lib/fixtures_issue_{number}_body.md", encoding="utf-8") as f:
        return f.read()


def entry(number, title):
    return {"number": number, "title": title, "url": f"https://github.com/o/r/issues/{number}", "body": read(number)}


pool_562 = [entry(562, "deploy-dsh-edge: автооткат (#549) оставляет рассинхрон версий"),
            entry(121, "Атомарная аренда задачи (task lease)")]
pool_518 = [entry(518, "deploy-dsh-edge.yml красный после бампа 0.11.1"),
            entry(121, "Атомарная аренда задачи (task lease)")]
pool_518_562 = [entry(518, "deploy-dsh-edge.yml красный после бампа 0.11.1"),
                entry(562, "deploy-dsh-edge: автооткат (#549) оставляет рассинхрон версий")]

with open(f"{work}/pool-562.json", "w", encoding="utf-8") as f:
    json.dump(pool_562, f, ensure_ascii=False)
with open(f"{work}/pool-518.json", "w", encoding="utf-8") as f:
    json.dump(pool_518, f, ensure_ascii=False)
with open(f"{work}/pool-518-562.json", "w", encoding="utf-8") as f:
    json.dump(pool_518_562, f, ensure_ascii=False)
PYEOF

# Пустой пул для ПЕРВОГО слоя (заголовок, #566) — изолирует тест от него,
# здесь проверяется только ВТОРОЙ слой (улики).
echo '[]' >"$WORK/title-empty.json"
export DUPLICATE_GUARD_FIXTURE="$WORK/title-empty.json"

cat >"$WORK/bin/gh" <<GHEOF
#!/usr/bin/env bash
if [ "\$1" = "repo" ] && [ "\$2" = "view" ]; then
  echo "o/r"
  exit 0
fi
if [ "\$1" = "issue" ] && [ "\$2" = "create" ]; then
  echo called >"$MARKER"
  args=("\$@")
  for idx in "\${!args[@]}"; do
    if [ "\${args[\$idx]}" = "--body" ]; then
      next=\$((idx + 1))
      printf '%s' "\${args[\$next]}" >"$CREATED_BODY"
    fi
    if [ "\${args[\$idx]}" = "--body-file" ]; then
      next=\$((idx + 1))
      cat "\${args[\$next]}" >"$CREATED_BODY"
    fi
  done
  echo "https://github.com/o/r/issues/999"
  exit 0
fi
echo "unexpected gh call: \$*" >&2
exit 1
GHEOF
chmod +x "$WORK/bin/gh"
export PATH="$WORK/bin:$PATH"

fail=0
note() { echo "$@"; }

# ── случай 1: #564 (дословно) против пула [#562, не связано] — отказ ────────
# Ловится ЧЕРЕЗ ДОСЛОВНУЮ ЦИТАТУ блока ``` (quote), не через ссылки/заголовок.
rm -f "$MARKER" "$CREATED_BODY"
export DUPLICATE_GUARD_EVIDENCE_FIXTURE="$WORK/pool-562.json"
if bash "$SCRIPT_SRC" --title "не важно, изолируем слой улик" --body "$BODY_564" --label task \
    >"$WORK/out1" 2>"$WORK/err1"; then
  note "FAIL случай 1: улика (цитата #562) принята без --confirm-not-duplicate"; fail=1
elif [ -f "$MARKER" ]; then
  note "FAIL случай 1: gh issue create был вызван, хотя улика есть"; fail=1
elif ! grep -q "562" "$WORK/err1"; then
  note "FAIL случай 1: кандидат #562 не назван в отказе"; cat "$WORK/err1"; fail=1
elif ! grep -q "цитата" "$WORK/err1"; then
  note "FAIL случай 1: причина (дословная цитата) не названа в отказе"; cat "$WORK/err1"; fail=1
else
  note "OK случай 1: улика (дословная цитата #562/#564) отклонена, gh не вызван"
fi

# ── случай 2: то же + --confirm-not-duplicate — принято, причина в теле ──────
rm -f "$MARKER" "$CREATED_BODY"
export DUPLICATE_GUARD_EVIDENCE_FIXTURE="$WORK/pool-562.json"
if ! bash "$SCRIPT_SRC" --title "не важно" --body "$BODY_564" --label task \
    --confirm-not-duplicate "разные причины отказа, не дубль" >"$WORK/out2" 2>"$WORK/err2"; then
  note "FAIL случай 2: --confirm-not-duplicate отклонён"; cat "$WORK/err2"; fail=1
elif [ ! -f "$MARKER" ]; then
  note "FAIL случай 2: gh issue create не вызван при явном --confirm-not-duplicate"; fail=1
elif ! grep -q "разные причины отказа, не дубль" "$CREATED_BODY"; then
  note "FAIL случай 2: причина --confirm-not-duplicate не попала в тело issue"; cat "$CREATED_BODY"; fail=1
elif ! grep -q "562" "$CREATED_BODY"; then
  note "FAIL случай 2: список проверенных кандидатов не попал в тело issue"; cat "$CREATED_BODY"; fail=1
else
  note "OK случай 2: --confirm-not-duplicate принят, причина и кандидат видны в теле issue"
fi

# ── случай 3: #548 (дословно) против пула [#518, не связано] — отказ ────────
# Ловится ЧЕРЕЗ ссылку «#518» в теле + общие ссылки #505/#513 (ref+self-cite),
# НЕ через дословную цитату (у этой пары нет общей ``` — честный предел #566
# первого слоя закрыт именно этим путём, см. scripts/lib/test_duplicate_guard.py).
rm -f "$MARKER" "$CREATED_BODY"
export DUPLICATE_GUARD_EVIDENCE_FIXTURE="$WORK/pool-518.json"
if bash "$SCRIPT_SRC" --title "не важно, изолируем слой улик" --body "$BODY_548" --label task \
    >"$WORK/out3" 2>"$WORK/err3"; then
  note "FAIL случай 3: улика (ссылка+самоцитата #518) принята без --confirm-not-duplicate"; fail=1
elif [ -f "$MARKER" ]; then
  note "FAIL случай 3: gh issue create был вызван, хотя улика есть"; fail=1
elif ! grep -q "518" "$WORK/err3"; then
  note "FAIL случай 3: кандидат #518 не назван в отказе"; cat "$WORK/err3"; fail=1
else
  note "OK случай 3: улика (ссылки #505/#513 + прямая цитата «#518») отклонена, gh не вызван"
fi

# ── случай 4: реальная, но НЕ связанная задача (#121) — принято без флага ────
rm -f "$MARKER" "$CREATED_BODY"
export DUPLICATE_GUARD_EVIDENCE_FIXTURE="$WORK/pool-518-562.json"
if ! bash "$SCRIPT_SRC" --title "совсем другая тема, не пересекается" --body "$BODY_UNRELATED" --label task \
    >"$WORK/out4" 2>"$WORK/err4"; then
  note "FAIL случай 4: топически не связанная задача (#121) отклонена"; cat "$WORK/err4"; fail=1
elif [ ! -f "$MARKER" ]; then
  note "FAIL случай 4: gh issue create не вызван для не связанной задачи"; fail=1
else
  note "OK случай 4: топически не связанная реальная задача (#121) принята без --confirm-not-duplicate"
fi

exit "$fail"
