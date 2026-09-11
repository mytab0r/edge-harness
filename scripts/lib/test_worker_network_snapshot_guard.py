#!/usr/bin/env python3
"""Гвардия класса «сетевой снимок под `set -euo pipefail` без обработки
отказа» (находка ai-review PR #937, задача #935, расширена вторым и третьим
проходом ревью на том же PR).

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
--jq .id)` без обработки отказа, scripts/worker/task.sh:456; третий проход
нашёл, что скан видел только `scripts/**/*.sh` — `scripts/git/task-branch`
(production bash-скрипт БЕЗ суффикса `.sh`) оставался невидим гвардии
целиком, хотя несёт свой голый `git ls-remote` (строка 98). Скан расширен на
экстеншн-less исполняемые bash-скрипты (см. `_looks_like_bash_script`).

Признак: строка производственного bash-скрипта (не test/*, не *.guard.sh/
*.smoke.sh) содержит один из MARKERS НЕ в комментарии — обязана либо нести
явную обработку отказа на той же логической строке (сама строка или
backslash-продолжение следующей: `|| die`, `|| {`, `|| ИМЯ=$?` — конвенция
явного захвата кода возврата с последующим `if`, scripts/git/task-branch:65),
либо стоять под условной веткой, которая уже проверяет код возврата
(`if ! VAR=$(...); then` — конвенция scripts/gh/wake_orchestra.sh). Голая
`VAR=$(...)` без того и другого — офендер.

scripts/git/task-branch:98 — живой офендер ВНЕ территории этой доводки
(параллельно открыты PR #970/#964/#952/#944/#941/#956, все трогающие
`scripts/git/*` — правка здесь рисковала бы конфликтом с чужой работой) —
занесён в KNOWN_OFFENDER_ALLOWLIST с обоснованием и ссылкой на issue #978
(заведена отдельно, area:process); ratchet (ALLOWLIST_RATCHET_MAX) не даёт
списку расти молча при будущих правках.

Тесты, смоук-фикстуры и сам этот гвардия-файл не сканируются — см.
_production_scripts().

Запуск: python -m pytest scripts/lib/test_worker_network_snapshot_guard.py -q
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

MARKERS = ("git ls-remote", "gh api")

# Живой офендер класса, закрытие которого — ВНЕ территории этой доводки (см.
# докстринг выше); задача-хвост — issue #978. Список СОКРАЩАЕТСЯ по мере
# починки — не растёт молча: ALLOWLIST_RATCHET_MAX фиксирует текущий верхний
# предел, как уже доказал себя тот же приём в
# scripts/lib/ci_guard_registration_guard.py.
KNOWN_OFFENDER_ALLOWLIST = frozenset({
    'scripts/git/task-branch:98 — remote_head=$(git ls-remote origin refs/heads/main | cut -f1)',
})
ALLOWLIST_RATCHET_MAX = 1


def _looks_like_bash_script(path: Path) -> bool:
    """Экстеншн-less файл — production bash-скрипт, если несёт bash/sh
    шебанг первой строкой (находка ai-review PR #937, третий проход:
    scripts/git/task-branch — как раз такой файл, `scripts/**/*.sh` его не
    видел вовсе)."""
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            first_line = fh.readline()
    except OSError:
        return False
    return bool(re.match(r"^#!.*\b(?:bash|sh)\b", first_line))


def _production_scripts() -> list[Path]:
    out = []
    for path in (REPO_ROOT / "scripts").rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(REPO_ROOT / "scripts")
        if "test" in rel.parts:
            continue
        if path.name.endswith((".guard.sh", ".smoke.sh")):
            continue
        if path.suffix == ".sh":
            out.append(path)
        elif path.suffix == "" and _looks_like_bash_script(path):
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
            # `&& rc=0 || rc=$?` — явный захват кода возврата с последующим
            # `if [ "$rc" -ne 0 ]; then ...` (конвенция scripts/git/
            # task-branch:64-65) — тот же уровень строгости, что `|| die`:
            # отказ не проглочен молча, он читаем следующей строкой.
            if re.search(r"\|\|\s*[A-Za-z_][A-Za-z0-9_]*=\$\?", logical):
                continue
            offenders.append(f"{rel}:{idx} — {line.strip()}")
    return offenders


def test_network_snapshots_have_failure_handling():
    offenders = _find_offenders()
    new_offenders = [o for o in offenders if o not in KNOWN_OFFENDER_ALLOWLIST]
    assert new_offenders == [], (
        "Сетевой снимок (git ls-remote/gh api) без обработки отказа в том "
        "же логическом выражении (класс — находка ai-review PR #937, "
        f"задача #935): {new_offenders}. Под `set -euo pipefail` голая "
        "подстановка на отказе сети роняет job без ::error:: и без отчёта — "
        'добавь `|| die "не смог ... (git/gh/сеть)"` (или `|| {`-блок, или '
        "условную ветку `if ! VAR=$(...); then`) по образцу "
        "scripts/worker/task.sh."
    )
    stale = KNOWN_OFFENDER_ALLOWLIST - set(offenders)
    assert not stale, (
        f"KNOWN_OFFENDER_ALLOWLIST несёт устаревшие записи, которых больше "
        f"нет в дереве: {stale} — почини (сократи список, не оставляй "
        "мёртвую запись)."
    )
    assert len(KNOWN_OFFENDER_ALLOWLIST) <= ALLOWLIST_RATCHET_MAX, (
        "KNOWN_OFFENDER_ALLOWLIST вырос без сознательного поднятия "
        "ALLOWLIST_RATCHET_MAX — новый обход класса не должен проходить "
        "молча аллоулистом вместо починки."
    )


# Мутации, которыми доказана гвардия (#937):
# 1) в scripts/worker/task.sh убери `\` и строку `  || die "..."` после
#    любого из двух `WORKER_BRANCH_ORIGIN_*_SHA=$(git ls-remote origin
#    "refs/heads/$BRANCH" | cut -f1)` — тест красный, верни `|| die` — снова
#    зелёный.
# 2) в scripts/worker/task.sh убери ` \` и строку `  || die "..."` после
#    `gh_user_id=$(gh api "users/$WORKER_LOGIN" --jq .id)` (находка второго
#    прохода ревью PR #937) — тест красный тем же способом, верни — зелёный.
# 3) третий проход: убери запись из KNOWN_OFFENDER_ALLOWLIST — тест красный
#    (assert stale красит на «устаревшую запись», раз офендер по-прежнему в
#    дереве); верни запись — снова зелёный. Убери суффикс `.sh`-only ветку
#    (закомментируй `elif path.suffix == "" and ...`) — тест красный на
#    отсутствии scripts/git/task-branch в скане; верни — зелёный.
