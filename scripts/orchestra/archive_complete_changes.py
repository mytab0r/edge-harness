#!/usr/bin/env python3
"""Инвариант 4 (#244) — автофикс НА ВХОДЕ, не гейт постфактум (issue #493).

Замер (2026-09-06, 06:00-12:00 UTC): 5 из ~15 падений `repo-ci.yml` подряд
красили ЧУЖИЕ PR текстом «инвариант 4 нарушен ... — снимается: перенеси
openspec/changes/<id> в openspec/changes/archive/» — где-то в main лежит
change с полностью отмеченным tasks.md, архивация которого — ручной шаг,
о котором вспоминают не все агенты. `repo_invariants.check_unarchived_
complete_changes` остаётся ЕДИНСТВЕННЫМ местом правды на критерий «полностью
отмечен» (импортируется отсюда, копия не заводится) — этот файл добавляет
FIX на той же ветке PR, где чекбоксы дозакрылись, ДО того как состояние
долетит до main и покрасит чужой прогон.

Запускается ТОЛЬКО job'ом repo-ci.yml на событие pull_request (не push на
main — прямой пуш в main отклоняется защитой, AGENTS.md: «Прямой пуш в main
не проходит обязательные проверки», единственный путь — agent-ветка + PR).
Перенос каталога — `git mv` (сохраняет историю); входящие markdown-ссылки на
старый путь чинятся В ТОМ ЖЕ коммите — иначе получаем битые указатели, класс,
уже пойманный на этом репозитории (AGENTS.md, «Правила, оплаченные чужими
ошибками»).

Идемпотентно: второй прогон на уже причёсанной ветке не находит нарушений
(check_unarchived_complete_changes) и ничего не коммитит.

Умышленно вызывает check_unarchived_complete_changes ТОЛЬКО с changes_dir
(быстрый путь «tasks.md полностью отмечен») — второй, независимый путь
завершённости (proposal.md + задача completed + нет открытого PR на путь,
docs/agents/OPENSPEC-PROTOCOL.md) сюда не передаётся нарочно. Причина: на
живом репозитории 2026-09-07 второй путь нашёл backlog в 23 каталога —
подключить его здесь означало бы, что ближайший же pull_request-прогон
archive-fixup молча закоммитит и запушит `git mv` по четверти всех change
без единого ревью. Массовая архивация — решение, требующее просмотра по
PR (план — OPENSPEC-PROTOCOL.md, раздел про backlog), не побочный эффект
чужого PR, который просто задел этот файл.

Инструментарий (этот файл, repo_invariants.py) исполняется из main-дерева
job'а — та же граблина, что #476 (`scripts/worker/task.sh`: доводка PR
исполняет scripts/* из main, а не из ветки PR): ветка PR, созданная ДО
появления этого файла, не содержит его вовсе, и `python scripts/orchestra/
archive_complete_changes.py` из чекаута ветки PR упал бы «No such file or
directory» (живой факт: прогон repo-ci.yml 34036104522 сразу после мержа
#493/PR #500, issue #506). Поэтому REPO_ROOT — `Path.cwd()`, НЕ расположение
этого файла: workflow обязан `cd` в linked git worktree ветки PR ПЕРЕД
вызовом (см. .github/workflows/repo-ci.yml, job archive-fixup) и вызывать
скрипт по абсолютному пути из main-дерева — ровно тот же приём, что
`scripts/worker/task.sh` применяет для доводки PR (`$PR_WORKTREE`).

Запуск (из целевого чекаута, cwd = дерево, которое нужно исправить):
  cd <дерево ветки PR> && python <main>/scripts/orchestra/archive_complete_changes.py
"""

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

_DIR = Path(__file__).resolve().parent
# REPO_ROOT — cwd вызова, НЕ расположение этого файла (см. докстринг выше):
# скрипт живёт в main-дереве job'а, а исправляет он дерево ветки PR, куда
# workflow обязан cd'нуться перед вызовом (linked git worktree, класс #476).
REPO_ROOT = Path.cwd()

# repo_invariants.py — единственное место правды на критерий «полностью
# отмечен и не заархивирован» (check_unarchived_complete_changes). importlib
# по АБСОЛЮТНОМУ пути расположения ЭТОГО файла (main-дерево, не REPO_ROOT) —
# тот же приём, что использует сам repo_invariants.py для pulse_guard/
# review_labels/task_ref/scheduler, и test_repo_invariants.py для себя самого.
# OPENSPEC_CHANGES НЕ берём из repo_invariants (тот считает путь от СВОЕГО
# расположения, т.е. main) — здесь он обязан указывать на REPO_ROOT (дерево
# ветки PR), иначе автофикс проверял бы и правил не то дерево.
_spec = importlib.util.spec_from_file_location("repo_invariants", _DIR / "repo_invariants.py")
repo_invariants = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(repo_invariants)  # type: ignore[union-attr]

check_unarchived_complete_changes = repo_invariants.check_unarchived_complete_changes
OPENSPEC_CHANGES = REPO_ROOT / "openspec" / "changes"

_SKIP_DIRS = frozenset({".git", "node_modules"})


def rewrite_inbound_links(repo_root: Path, name: str) -> dict[Path, str]:
    """Markdown-файлы репозитория (кроме тех, что лежат ВНУТРИ самого
    переносимого каталога — он уже переехал на новый путь, свои внутренние
    относительные ссылки не задевает), содержащие литеральный путь
    `openspec/changes/<name>`, переписанный на `openspec/changes/archive/
    <name>`. Граница `(?![\\w-])` — не задеть `openspec/changes/<name>-suffix`
    другого change с похожим именем (класс, аналогичный check_duplicate_
    evidence: точное совпадение пути, не префикс). Чистая функция (без
    записи на диск) — тестируется на temp-дереве без реального git."""
    old_rel = f"openspec/changes/{name}"
    new_rel = f"openspec/changes/archive/{name}"
    skip_prefix = new_rel + "/"
    pattern = re.compile(re.escape(old_rel) + r"(?![\w-])")

    updated: dict[Path, str] = {}
    for path in sorted(repo_root.rglob("*.md")):
        if set(path.parts) & _SKIP_DIRS:
            continue
        rel = path.relative_to(repo_root).as_posix()
        if rel.startswith(skip_prefix) or rel == f"{new_rel}.md":
            continue
        text = path.read_text(encoding="utf-8")
        new_text = pattern.sub(new_rel, text)
        if new_text != text:
            updated[path] = new_text
    return updated


def archive_one(repo_root: Path, name: str) -> list[str]:
    """git mv <name> в archive/, чинит входящие ссылки (см. rewrite_inbound_
    links), возвращает строки отчёта для commit message/лога."""
    old_dir = repo_root / "openspec" / "changes" / name
    new_dir = repo_root / "openspec" / "changes" / "archive" / name
    new_dir.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "mv", str(old_dir), str(new_dir)], cwd=repo_root, check=True)

    updated = rewrite_inbound_links(repo_root, name)
    for path, text in updated.items():
        path.write_text(text, encoding="utf-8")

    lines = [f"openspec/changes/{name} -> openspec/changes/archive/{name}"]
    for path in sorted(updated):
        lines.append(f"  починена ссылка: {path.relative_to(repo_root).as_posix()}")
    return lines


def configure_git_identity(repo_root: Path) -> None:
    """Коммит от лица владельца, не github-actions[bot] (тот же приём, что
    scripts/worker/task.sh): noreply-адрес формируется из настоящего id
    аккаунта, под которым авторизован secrets.GH_PIPELINE_PAT в этом job'е —
    единственный источник identity, второй копией здесь не заводим."""
    who = subprocess.run(
        ["gh", "api", "user", "--jq", "[.login, (.id|tostring)] | @tsv"],
        cwd=repo_root, check=True, capture_output=True, text=True,
    ).stdout.strip()
    login, user_id = who.split("\t")
    subprocess.run(["git", "config", "user.name", login], cwd=repo_root, check=True)
    subprocess.run(
        ["git", "config", "user.email", f"{user_id}+{login}@users.noreply.github.com"],
        cwd=repo_root, check=True,
    )


def main() -> int:
    # Регрессия #506: печатает разрешённый REPO_ROOT и выходит без git/gh —
    # используется только тестом (см. test_repo_root_comes_from_cwd_not_
    # from_script_location), доказывающим, что REPO_ROOT берётся из cwd
    # вызова, а не из расположения этого файла.
    if len(sys.argv) > 1 and sys.argv[1] == "--print-repo-root-for-test":
        print(REPO_ROOT)
        return 0

    violations = check_unarchived_complete_changes(OPENSPEC_CHANGES)
    if not violations:
        print("archive_complete_changes: нечего архивировать")
        return 0

    report: list[str] = []
    for item in violations:
        report.extend(archive_one(REPO_ROOT, item["change"]))

    subprocess.run(["git", "add", "-A"], cwd=REPO_ROOT, check=True)
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=REPO_ROOT,
        capture_output=True, text=True, check=True,
    ).stdout
    if not status.strip():
        print("archive_complete_changes: git mv не дал диффа (уже применено) — no-op")
        return 0

    configure_git_identity(REPO_ROOT)
    message = (
        "openspec: автоархивация полностью выполненных change (инвариант 4, #493)\n\n"
        + "\n".join(report)
    )
    subprocess.run(["git", "commit", "-m", message], cwd=REPO_ROOT, check=True)
    subprocess.run(["git", "push"], cwd=REPO_ROOT, check=True)
    print("archive_complete_changes: заархивировано и запушено:\n" + "\n".join(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
