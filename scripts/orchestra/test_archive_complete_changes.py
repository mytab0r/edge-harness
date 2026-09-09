#!/usr/bin/env python3
"""Тесты автофикса инварианта 4 (scripts/orchestra/archive_complete_changes.py,
issue #493) — чистая часть (rewrite_inbound_links): переписывание входящих
markdown-ссылок при переносе openspec/changes/<id> в archive/, без реального
git. Мутация: убери границу `(?![\\w-])` — тест на префиксный false-positive
должен покраснеть; убери фильтр skip_prefix — тест «собственные файлы не
трогаются» должен покраснеть.

Плюс регрессия #506 (живой факт: прогон repo-ci.yml 34036104522, сразу после
мержа PR #500, — «No such file or directory» на ветке PR старше появления
этого файла): REPO_ROOT обязан браться из cwd вызова, а не из расположения
самого файла — иначе job archive-fixup ломается на КАЖДОЙ ветке, созданной до
появления скрипта. Мутация: верни `REPO_ROOT = _DIR.parents[1]` — тест
краснеет.

Запуск: python -m pytest scripts/orchestra/test_archive_complete_changes.py -q
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

_DIR = Path(__file__).resolve().parent
SCRIPT = _DIR / "archive_complete_changes.py"
spec = importlib.util.spec_from_file_location("archive_complete_changes", SCRIPT)
acc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acc)  # type: ignore[union-attr]


def test_repo_root_comes_from_cwd_not_from_script_location(tmp_path: Path):
    # Регрессия #506: скрипт живёт в main-дереве job'а, но обязан править
    # ДРУГОЕ дерево (linked worktree ветки PR), в которое workflow cd'нулся
    # перед вызовом. Прогон в отдельном процессе с cwd = tmp_path (внутри
    # него НЕТ scripts/orchestra/archive_complete_changes.py вовсе — та же
    # форма, что ветка PR старше появления этого файла) — REPO_ROOT обязан
    # резолвиться в tmp_path, а не в scripts/orchestra (расположение файла),
    # и импорт не должен падать на отсутствии файла в cwd.
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--print-repo-root-for-test"],
        cwd=tmp_path, capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    printed_root = Path(result.stdout.strip())
    assert printed_root.resolve() == tmp_path.resolve(), (
        f"REPO_ROOT резолвился в {printed_root}, ожидался cwd вызова {tmp_path} — "
        "regressия #506 (job archive-fixup падает на ветке PR старше появления скрипта)"
    )


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_rewrites_inbound_link_to_archive_path(tmp_path: Path):
    doc = tmp_path / "docs" / "INDEX.md"
    write(doc, "Смотри openspec/changes/walking-skeleton/design.md для контекста.\n")
    write(tmp_path / "openspec" / "changes" / "walking-skeleton" / "proposal.md", "# ok\n")

    updated = acc.rewrite_inbound_links(tmp_path, "walking-skeleton")

    assert doc in updated
    assert "openspec/changes/archive/walking-skeleton/design.md" in updated[doc]
    assert "openspec/changes/walking-skeleton/design.md" not in updated[doc]


def test_does_not_touch_file_with_no_reference(tmp_path: Path):
    doc = tmp_path / "docs" / "unrelated.md"
    write(doc, "Ничего про openspec здесь нет.\n")

    updated = acc.rewrite_inbound_links(tmp_path, "walking-skeleton")

    assert updated == {}


def test_does_not_false_positive_on_prefix_collision(tmp_path: Path):
    # openspec/changes/walking-skeleton-v2 не должен матчиться при переносе
    # walking-skeleton — иначе перенос одного change ломает ссылку на другой.
    doc = tmp_path / "docs" / "INDEX.md"
    write(doc, "Активная стройка: openspec/changes/walking-skeleton-v2/proposal.md\n")

    updated = acc.rewrite_inbound_links(tmp_path, "walking-skeleton")

    assert updated == {}


def test_skips_files_already_inside_the_moved_directory(tmp_path: Path):
    # Файлы внутри самого archive/<name>/ (например design.md, ссылающийся на
    # соседний proposal.md полным путём) не переписываются этой функцией —
    # git mv уже перенёс их физически, self-ссылки живут относительным путём.
    already_moved = tmp_path / "openspec" / "changes" / "archive" / "walking-skeleton" / "design.md"
    write(already_moved, "См. openspec/changes/walking-skeleton/proposal.md\n")

    updated = acc.rewrite_inbound_links(tmp_path, "walking-skeleton")

    assert already_moved not in updated


def test_idempotent_second_pass_finds_nothing(tmp_path: Path):
    doc = tmp_path / "docs" / "INDEX.md"
    write(doc, "Смотри openspec/changes/walking-skeleton/design.md.\n")

    first = acc.rewrite_inbound_links(tmp_path, "walking-skeleton")
    doc.write_text(first[doc], encoding="utf-8")

    second = acc.rewrite_inbound_links(tmp_path, "walking-skeleton")

    assert second == {}


def test_main_calls_checker_with_fast_path_only():
    # Умышленное ограничение области (см. докстринг модуля): main() обязан
    # звать check_unarchived_complete_changes ТОЛЬКО с changes_dir — второй,
    # независимый путь завершённости (proposal.md + задача completed + нет
    # открытого PR) требует сетевых данных (task_states/open_pull_texts) и
    # на живом репозитории 2026-09-07 нашёл backlog в 23 каталога. Подключить
    # его сюда означало бы, что archive-fixup (job на КАЖДЫЙ pull_request)
    # молча закоммитит и запушит git mv по четверти всех change без ревью —
    # массовая архивация обязана идти отдельными просмотренными PR, не
    # побочным эффектом чужого пуша. Мутация: допиши второй позиционный
    # аргумент в вызове ниже в archive_complete_changes.py — тест краснеет.
    source = (Path(__file__).resolve().parent / "archive_complete_changes.py").read_text(
        encoding="utf-8"
    )
    assert "check_unarchived_complete_changes(OPENSPEC_CHANGES)" in source, (
        "main() обязан звать check_unarchived_complete_changes с ОДНИМ аргументом "
        "(changes_dir) — второй путь завершённости не должен молча запускать "
        "автокоммит/пуш по backlog'у, см. докстринг archive_complete_changes.py"
    )
