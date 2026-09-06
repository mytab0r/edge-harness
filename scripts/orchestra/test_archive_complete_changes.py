#!/usr/bin/env python3
"""Тесты автофикса инварианта 4 (scripts/orchestra/archive_complete_changes.py,
issue #493) — только чистая часть (rewrite_inbound_links): переписывание
входящих markdown-ссылок при переносе openspec/changes/<id> в archive/, без
реального git. Мутация: убери границу `(?![\\w-])` — тест на префиксный
false-positive должен покраснеть; убери фильтр skip_prefix — тест
«собственные файлы не трогаются» должен покраснеть.

Запуск: python -m pytest scripts/orchestra/test_archive_complete_changes.py -q
"""

import importlib.util
from pathlib import Path

_DIR = Path(__file__).resolve().parent
SCRIPT = _DIR / "archive_complete_changes.py"
spec = importlib.util.spec_from_file_location("archive_complete_changes", SCRIPT)
acc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acc)  # type: ignore[union-attr]


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
