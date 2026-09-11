#!/usr/bin/env python3
"""Гвардия класса «сетевой снимок под `set -euo pipefail` без `|| die`»
(находка ai-review PR #937, задача #935).

Класс: `git ls-remote origin ...` — сетевой запрос, читаемый через
`VAR=$(...)` в скрипте с `set -euo pipefail` (scripts/worker/task.sh). Под
pipefail отказ сети/API делает саму подстановку ненулевой — без явного
`|| die "..."` errexit убивает job голой bash-ошибкой строки, без
`::error::`, без `WORKER_TASK_FAILURE_REASON`, без отчёта (тот же класс, что
уже закрыт для соседних сетевых чтений того же файла — `gh pr list`/
`gh issue view`, конвенция `|| die "не смог прочитать ... (gh/сеть)"`,
scripts/worker/task.sh). PR #937 добавил `git ls-remote` дважды (снимок
головы ветки на origin до/после прогона dsh, регрессия #878/#935) — до
фикса обе строки были голыми подстановками без обработки отказа.

Признак: строка производственного .sh-скрипта (не test/*, не *.guard.sh/
*.smoke.sh) содержит `git ls-remote` НЕ в комментарии — обязана нести
`|| die` на той же логической строке (сама строка или backslash-продолжение
следующей).

Тесты, смоук-фикстуры и сам этот гвардия-файл не сканируются — см.
_production_scripts().

Запуск: python -m pytest scripts/lib/test_worker_network_snapshot_guard.py -q
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

MARKER = "git ls-remote"


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


def _find_offenders() -> list[str]:
    offenders = []
    for path in _production_scripts():
        rel = str(path.relative_to(REPO_ROOT)).replace("\\", "/")
        lines = path.read_text(encoding="utf-8").splitlines()
        for idx, line in enumerate(lines, start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if MARKER not in line:
                continue
            # Логическая строка — сама строка плюс, если она продолжается
            # backslash'ем, следующая (ровно то, как оформлен фикс #937:
            # `VAR=$(git ls-remote ...) \` + `  || die "..."`).
            logical = line
            if stripped.endswith("\\") and idx < len(lines):
                logical = line + "\n" + lines[idx]
            if "|| die" not in logical:
                offenders.append(f"{rel}:{idx} — {line.strip()}")
    return offenders


def test_ls_remote_snapshots_have_die_fallback():
    offenders = _find_offenders()
    assert offenders == [], (
        "Сетевой снимок `git ls-remote` без `|| die` в том же логическом "
        "выражении (класс — находка ai-review PR #937, задача #935): "
        f"{offenders}. Под `set -euo pipefail` голая подстановка на отказе "
        "сети роняет job без ::error:: и без отчёта — добавь "
        '`|| die "не смог снять снимок головы ветки ... (git/сеть)"` по '
        "образцу scripts/worker/task.sh."
    )


# Мутация, которой доказана гвардия (#937): в scripts/worker/task.sh убери
# `\` и строку `  || die "..."` после любого из двух
# `WORKER_BRANCH_ORIGIN_*_SHA=$(git ls-remote origin "refs/heads/$BRANCH" |
# cut -f1)` — этот тест красный. Верни `|| die "..."` — тест снова зелёный.
