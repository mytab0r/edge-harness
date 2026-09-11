#!/usr/bin/env python3
"""Гвардия класса «сетевой снимок под `set -euo pipefail` без обработки
отказа» (находка ai-review PR #937, задача #935, расширена вторым проходом
ревью на том же PR).

Класс: сетевой запрос (`git ls-remote ...`, `gh api ...`), читаемый через
`VAR=$(...)` в скрипте с `set -euo pipefail`. Под pipefail отказ сети/API
делает саму подстановку ненулевой — без явной обработки errexit убивает job
голой bash-ошибкой строки, без `::error::`, без `WORKER_TASK_FAILURE_REASON`,
без отчёта (тот же класс, что уже закрыт для соседних сетевых чтений того же
файла — `gh pr list`/`gh issue view`, конвенция
`|| die "не смог прочитать ... (gh/сеть)"`, scripts/worker/task.sh). PR #937
первым проходом закрыл `git ls-remote` (снимок головы ветки на origin
до/после прогона dsh, регрессия #878/#935); второй проход ревью нашёл живое
второе место того же класса — голый `gh_user_id=$(gh api "users/$WORKER_LOGIN"
--jq .id)` без обработки отказа, scripts/worker/task.sh:456 — маркер
расширен, чтобы не пропускать этот вариант класса снова.

Признак: строка производственного .sh-скрипта (не test/*, не *.guard.sh/
*.smoke.sh) содержит один из MARKERS НЕ в комментарии — обязана либо нести
явную обработку отказа на той же логической строке (сама строка или
backslash-продолжение следующей: `|| die`, `|| {`), либо стоять под условной
веткой, которая уже проверяет код возврата (`if ! VAR=$(...); then` — конвенция
scripts/gh/wake_orchestra.sh). Голая `VAR=$(...)` без того и другого — офендер.

Тесты, смоук-фикстуры и сам этот гвардия-файл не сканируются — см.
_production_scripts().

Запуск: python -m pytest scripts/lib/test_worker_network_snapshot_guard.py -q
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

MARKERS = ("git ls-remote", "gh api")


def _production_scripts() -> list[Path]:
    out = []
    for path in (REPO_ROOT / "scripts").rglob("*.sh"):
        rel = path.relative_to(REPO_ROOT / "scripts")
        if "test" in rel.parts:
            continue
        if path.name.endswith((".guard.sh", ".smoke.sh")):
            continue
        out.append(path)
    return out


def _is_invocation(line: str, marker: str) -> bool:
    """`marker` встречается как реальный запуск команды (начало строки или
    подстановка `$(...)`/`` `...` ``), а не как текст внутри `echo`/heredoc-
    документации (живые ложные срабатывания: scripts/gh/infra_digest.sh —
    heredoc с описанием инфраструктуры, scripts/gh/rate.sh — текст сообщения
    об ошибке `echo "... gh api rate_limit ..." >&2`, обе строки упоминают
    `gh api` как слова для человека, не как вызов)."""
    stripped = line.strip()
    return (
        stripped.startswith(marker)
        or f"$({marker}" in line
        or f"`{marker}" in line
    )


def _find_offenders() -> list[str]:
    offenders = []
    for path in _production_scripts():
        rel = str(path.relative_to(REPO_ROOT)).replace("\\", "/")
        lines = path.read_text(encoding="utf-8").splitlines()
        for idx, line in enumerate(lines, start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if not any(_is_invocation(line, marker) for marker in MARKERS):
                continue
            # Уже под условной веткой, проверяющей код возврата — конвенция
            # `if ! VAR=$(...); then` (scripts/gh/wake_orchestra.sh) — отказ
            # обработан явно самим ветвлением, второй `|| ...` не нужен.
            if stripped.startswith(("if ", "elif ")):
                continue
            # Логическая строка — сама строка плюс, если она продолжается
            # backslash'ем, следующая (ровно то, как оформлен фикс #937:
            # `VAR=$(git ls-remote ...) \` + `  || die "..."`).
            logical = line
            if stripped.endswith("\\") and idx < len(lines):
                logical = line + "\n" + lines[idx]
            if "|| die" in logical or "|| {" in logical:
                continue
            offenders.append(f"{rel}:{idx} — {line.strip()}")
    return offenders


def test_network_snapshots_have_failure_handling():
    offenders = _find_offenders()
    assert offenders == [], (
        "Сетевой снимок (git ls-remote/gh api) без обработки отказа в том "
        "же логическом выражении (класс — находка ai-review PR #937, "
        f"задача #935): {offenders}. Под `set -euo pipefail` голая "
        "подстановка на отказе сети роняет job без ::error:: и без отчёта — "
        'добавь `|| die "не смог ... (git/gh/сеть)"` (или `|| {`-блок, или '
        "условную ветку `if ! VAR=$(...); then`) по образцу "
        "scripts/worker/task.sh."
    )


# Мутации, которыми доказана гвардия (#937):
# 1) в scripts/worker/task.sh убери `\` и строку `  || die "..."` после
#    любого из двух `WORKER_BRANCH_ORIGIN_*_SHA=$(git ls-remote origin
#    "refs/heads/$BRANCH" | cut -f1)` — тест красный, верни `|| die` — снова
#    зелёный.
# 2) в scripts/worker/task.sh убери ` \` и строку `  || die "..."` после
#    `gh_user_id=$(gh api "users/$WORKER_LOGIN" --jq .id)` (находка второго
#    прохода ревью PR #937) — тест красный тем же способом, верни — зелёный.
