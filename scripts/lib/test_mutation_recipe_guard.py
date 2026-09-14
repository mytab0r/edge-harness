#!/usr/bin/env python3
"""Тесты scripts/lib/mutation_recipe_guard.py (issue #1194).

Запуск: python -m pytest scripts/lib/test_mutation_recipe_guard.py -q

Ключевой тест здесь — `test_historical_1173_prefix_claim_is_unconfirmed_1165_
claim_is_confirmed`: он реально исполняет мутацию на РЕАЛЬНЫХ коммитах main
(`d239e324~1` — до фикса #1163, `d239e324` — сам мерж-коммит фикса), той же
парой значений, что разошлись в живом инциденте issue #1194 (заявлено «1
освобождение, третья пачка не запускается»; факт — «3 освобождения, третья
пачка выполняется и падает»). Требует `node` в PATH (тот же фикстурный
раннер, что дал бы дословный вывод человеку) — пропускается честно, если
node недоступен, не притворяется зелёным.
"""

from __future__ import annotations

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
import shutil
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import subprocess

import pytest

_MRG_SPEC = importlib.util.spec_from_file_location(
    "mutation_recipe_guard", Path(__file__).resolve().parent / "mutation_recipe_guard.py")
mrg = importlib.util.module_from_spec(_MRG_SPEC)
_MRG_SPEC.loader.exec_module(mrg)  # type: ignore[union-attr]

_CHR_SPEC = importlib.util.spec_from_file_location(
    "check_result", Path(__file__).resolve().parent / "check_result.py")
check_result = importlib.util.module_from_spec(_CHR_SPEC)
_CHR_SPEC.loader.exec_module(check_result)  # type: ignore[union-attr]

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURE_RUNNER = "scripts/lib/test/ingest-mutation-scenario.mjs"
PATCH_PATH = "dsh-edge/patches/0004-harness-ingest.patch"


# --- парсер --------------------------------------------------------------

def test_parse_finds_a_well_formed_block():
    text = """
// какой-то код
// MUTATION-PROOF
// ref: abc123
// paths: dsh-edge/patches/0004-harness-ingest.patch
// run: node script.mjs
// expect: disposeCalls=3
import x from 'y'
"""
    blocks = mrg.parse_mutation_proof_blocks(text, source_file="f.mjs")
    assert len(blocks) == 1
    b = blocks[0]
    assert b.ref == "abc123"
    assert b.paths == ("dsh-edge/patches/0004-harness-ingest.patch",)
    assert b.run == "node script.mjs"
    assert b.expect == "disposeCalls=3"


def test_parse_ignores_incomplete_block_missing_expect():
    text = """
# MUTATION-PROOF
# ref: abc123
# paths: some/file.py
# run: pytest -q
"""
    assert mrg.parse_mutation_proof_blocks(text) == []


def test_parse_finds_multiple_paths_comma_separated():
    text = """
# MUTATION-PROOF
# ref: HEAD~1
# paths: a/b.py, c/d.py
# run: pytest -q
# expect: 2 failed
"""
    blocks = mrg.parse_mutation_proof_blocks(text)
    assert blocks[0].paths == ("a/b.py", "c/d.py")


def test_parse_returns_empty_for_text_without_marker():
    assert mrg.parse_mutation_proof_blocks("обычный текст без блока") == []


# --- verify_block: unknown() на несуществующем ref ------------------------

def test_verify_block_unknown_on_missing_ref():
    block = mrg.MutationProofBlock(
        source_file="x", line_no=1, ref="0000000000000000000000000000000000000000",
        paths=("README.md",), run="true", expect="whatever",
    )
    result = mrg.verify_block(block, repo_root=REPO_ROOT)
    assert result.status == check_result.STATUS_UNKNOWN
    assert "не читается" in result.reason


# --- исторический случай #1173/#1165 (issue #1194) — РЕАЛЬНОЕ исполнение --

def _node_available() -> bool:
    return shutil.which("node") is not None


def _ref_exists(ref: str) -> bool:
    proc = subprocess.run(
        ["git", "cat-file", "-e", f"{ref}^{{commit}}"],
        cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    return proc.returncode == 0


@pytest.mark.skipif(not _node_available(), reason="node недоступен в PATH — фикстурный раннер не исполнить")
@pytest.mark.skipif(not _ref_exists("d239e324~1"), reason="история репозитория не несёт d239e324 (мелкий/частичный клон?)")
def test_historical_1173_prefix_claim_is_unconfirmed_1165_claim_is_confirmed():
    """Заявленное в PR #1173 (коммит cb854b49, ДО правки по ai-review):
    «disposeCalls is 1» и «batch3 never runs» — не подтверждается исполнением
    на РЕАЛЬНОМ коде до фикса #1163 (d239e324~1): факт — 3 освобождения,
    третья пачка ВЫПОЛНЯЕТСЯ и падает. Исправленное заявление (ea5cf344):
    «disposeCalls is 3 (not 0)» — подтверждается тем же самым исполнением.
    """
    claimed_wrong = mrg.MutationProofBlock(
        source_file="issue-1194-fixture", line_no=1,
        ref="d239e324~1", paths=(PATCH_PATH,),
        run=f"node {FIXTURE_RUNNER} {PATCH_PATH}",
        expect="disposeCalls=1",
    )
    result_wrong = mrg.verify_block(claimed_wrong, repo_root=REPO_ROOT)
    assert result_wrong.status == check_result.STATUS_VIOLATION, (
        f"ожидался VIOLATION (рецепт по аналогии не подтверждён), получено {result_wrong}")
    assert "не исполнен" in result_wrong.violations[0] or "НЕ содержит" in result_wrong.violations[0]

    claimed_right = mrg.MutationProofBlock(
        source_file="issue-1194-fixture", line_no=1,
        ref="d239e324~1", paths=(PATCH_PATH,),
        run=f"node {FIXTURE_RUNNER} {PATCH_PATH}",
        expect="disposeCalls=3",
    )
    result_right = mrg.verify_block(claimed_right, repo_root=REPO_ROOT)
    assert result_right.status == check_result.STATUS_OK, (
        f"ожидался OK (рецепт совпал с фактом), получено {result_right}")


@pytest.mark.skipif(not _node_available(), reason="node недоступен в PATH")
@pytest.mark.skipif(not _ref_exists("d239e324~1"), reason="история репозитория не несёт d239e324")
def test_expect_present_even_without_mutation_is_flagged_as_vacuous():
    """`batch1.appended=2` печатается ОДИНАКОВО и до, и после фикса — если бы
    кто-то (ошибочно) взял его как `expect`, гвардия обязана заметить, что
    мутация ничего не отличает, а не тихо засчитать совпадение как успех."""
    vacuous = mrg.MutationProofBlock(
        source_file="issue-1194-fixture", line_no=1,
        ref="d239e324~1", paths=(PATCH_PATH,),
        run=f"node {FIXTURE_RUNNER} {PATCH_PATH}",
        expect="batch1.appended=2",
    )
    result = mrg.verify_block(vacuous, repo_root=REPO_ROOT)
    assert result.status == check_result.STATUS_VIOLATION
    assert "ничего не отличает" in result.violations[0]


# --- ноль ложных срабатываний на последних merged PR (opt-in формат) ------

def test_scan_of_last_20_merged_pr_test_files_finds_zero_blocks_and_zero_violations(tmp_path):
    """Формат MUTATION-PROOF добровольный (opt-in) — ни один из тест-файлов,
    затронутых последними ~20 слитыми PR с прозаичным «Доказательство
    мутацией», сегодня не несёт структурированного блока. Сканирование этих
    файлов обязано возвращать 0 блоков и, значит, 0 нарушений — не
    ретроактивное требование ко всему корпусу прозы."""
    candidate_files = [
        "scripts/orchestra/test_reachability_guard.py",
        "scripts/orchestra/test_pulse_guard.py",
        "scripts/orchestra/test_scheduler.py",
        "scripts/lib/test_check_result_migrations.py",
        "scripts/orchestra/test_stale_blocked_guard.py",
        "scripts/lib/test_provider_quota_state_guard.py",
        "scripts/orchestra/test_repo_invariants.py",
        "dsh-edge/test/ingest-resident-safety.test.mjs",
    ]
    existing = [REPO_ROOT / f for f in candidate_files if (REPO_ROOT / f).is_file()]
    assert len(existing) >= 5, "фикстура протухла — ни один из ожидаемых файлов не найден на диске"
    results = mrg.scan_and_verify(existing, repo_root=REPO_ROOT)
    assert results == [], (
        f"ожидалось 0 блоков MUTATION-PROOF в корпусе прозаичных рецептов, найдено {len(results)}")
