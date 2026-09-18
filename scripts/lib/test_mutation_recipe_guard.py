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


def test_parse_incomplete_block_excluded_but_reported_not_silently_dropped():
    """Маркер без всех ключей не становится исполняемым MutationProofBlock
    (нечем исполнять), НО и не исчезает бесследно — раньше исчезал молча
    (issue #1208, второй круг ai-review на PR #1208): `parse_incomplete_
    markers` обязан назвать line_no и недостающий ключ."""
    text = """
# MUTATION-PROOF
# ref: abc123
# paths: some/file.py
# run: pytest -q
"""
    assert mrg.parse_mutation_proof_blocks(text) == []
    incomplete = mrg.parse_incomplete_markers(text, source_file="f.py")
    assert len(incomplete) == 1
    assert incomplete[0].source_file == "f.py"
    assert incomplete[0].line_no == 2
    assert incomplete[0].missing_keys == ("expect",)


def test_scan_and_verify_reports_incomplete_block_as_violation(tmp_path):
    """Тот же случай на уровне `scan_and_verify` (реальный путь вызова из
    `main()`/каталог-гвардии) — неполный маркер обязан красить проверку, а не
    молча давать 0 результатов."""
    f = tmp_path / "recipe.py"
    f.write_text(
        "# MUTATION-PROOF\n"
        "# ref: abc123\n"
        "# paths: some/file.py\n"
        "# run: pytest -q\n",
        encoding="utf-8",
    )
    results = mrg.scan_and_verify([f], repo_root=tmp_path)
    assert len(results) == 1
    block, result = results[0]
    assert isinstance(block, mrg.IncompleteMutationProofBlock)
    assert result.status == check_result.STATUS_VIOLATION
    assert "не хватает ключей" in result.violations[0]
    assert "expect" in result.violations[0]


def test_parse_finds_marker_wrapped_in_html_comment_close():
    """`<!-- MUTATION-PROOF -->` — естественное чтение markdown-документации
    ADR/задач, где рецепт живёт внутри html-комментария (issue #1208, второй
    круг ai-review: эта форма раньше давала 0 блоков)."""
    text = """
<!-- MUTATION-PROOF -->
<!-- ref: abc123 -->
<!-- paths: some/file.py -->
<!-- run: pytest -q -->
<!-- expect: 2 failed -->
"""
    blocks = mrg.parse_mutation_proof_blocks(text, source_file="f.md")
    assert len(blocks) == 1
    b = blocks[0]
    assert b.ref == "abc123"
    assert b.paths == ("some/file.py",)
    assert b.run == "pytest -q"
    assert b.expect == "2 failed"


def test_parse_finds_marker_wrapped_in_markdown_bold():
    """`**MUTATION-PROOF**` — вторая форма, найденная тем же кругом ревью."""
    text = """
**MUTATION-PROOF**
ref: abc123
paths: some/file.py
run: pytest -q
expect: 2 failed
"""
    blocks = mrg.parse_mutation_proof_blocks(text)
    assert len(blocks) == 1


def test_parse_tolerates_blank_line_between_marker_and_keys():
    """Пустая строка между маркером и первым ключом (markdown-рендер таблиц/
    списков иногда её вставляет) раньше обрывала блок молча — issue #1208."""
    text = """
# MUTATION-PROOF

# ref: abc123
# paths: some/file.py
# run: pytest -q
# expect: 2 failed
"""
    blocks = mrg.parse_mutation_proof_blocks(text)
    assert len(blocks) == 1


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


# --- достижимость на ПРОД-величинах (issue #1208, #1172) ------------------

def test_own_docstring_and_adr_placeholder_examples_are_not_live_blocks():
    """Доказательство на настоящем содержимом файлов, не на пересказе (issue
    #1208): буквальная рекомендация ai-review («скани дерево одной строкой»)
    без фильтра ловит placeholder-пример формата в НАСТОЯЩЕМ докстринге этого
    же модуля и в НАСТОЯЩЕМ ADR 0023 как валидный блок с ref, который git
    никогда не примет (пробел внутри) — оба давали бы вечный unknown() и
    красили каталог-гвардию шумом при каждом прогоне."""
    guard_src = (REPO_ROOT / "scripts/lib/mutation_recipe_guard.py").read_text(encoding="utf-8")
    adr = (REPO_ROOT / "docs/decisions/0023-mutation-recipe-execution-guard.md").read_text(encoding="utf-8")
    for text, name in [(guard_src, "mutation_recipe_guard.py"), (adr, "ADR 0023")]:
        blocks, incomplete = mrg._scan_blocks(text, name)
        assert blocks == [], f"{name}: докстринг/ADR-пример формата не должен парситься как живой блок"
        assert incomplete == [], f"{name}: докстринг/ADR-пример не должен попадать даже в incomplete"


@pytest.mark.skipif(not shutil.which("node"), reason="node недоступен в PATH — фикстурный раннер не исполнить")
@pytest.mark.skipif(
    subprocess.run(["git", "cat-file", "-e", "d239e324~1^{commit}"], cwd=REPO_ROOT,
                    capture_output=True).returncode != 0,
    reason="история репозитория не несёт d239e324 (мелкий/частичный клон?)")
def test_block_inside_realistic_pr_comment_body_is_recognized_and_executes():
    """Формат распознаётся не в синтетической фикстуре парсера, а в теле,
    какое реально пишет автор PR/комментария — markdown-прозой вокруг, без
    единого символа комментария (issue #1208, #1172: достижимость на
    прод-величинах). Блок берётся из реального PR-тела и реально
    исполняется — не только парсится."""
    pr_comment_body = f"""
## Доказательство мутацией

Откатил патч до состояния ДО фикса #1163 и прогнал фикстуру заново.

MUTATION-PROOF
ref: d239e324~1
paths: {PATCH_PATH}
run: node {FIXTURE_RUNNER} {PATCH_PATH}
expect: disposeCalls=3

Вывод совпал с тем, что описан в ADR 0023.
"""
    blocks = mrg.parse_mutation_proof_blocks(pr_comment_body, source_file="pr-comment")
    assert len(blocks) == 1
    assert blocks[0].ref == "d239e324~1"
    result = mrg.verify_block(blocks[0], repo_root=REPO_ROOT)
    assert result.status == check_result.STATUS_OK, f"ожидался OK, получено {result}"


# --- main(): unknown — отдельный счётчик, не «проверено» ------------------

def test_main_reports_unknown_separately_not_as_checked(capsys):
    """До фикса (issue #1208, второй круг ai-review, находка 2): `main()`
    считал unknown() в то же «N блок(ов) проверено, 0 расхождений» и
    возвращал 0 — «не проверен» выглядел как «чисто». Реальный прогон CLI
    (не пересказ поведения) на несуществующем ref обязан вернуть отдельный
    ненулевой код и не написать «проверено» про непроверенный блок."""
    scratch = REPO_ROOT / "scripts" / "lib" / "test" / "_scratch_mutation_proof_main_test.txt"
    scratch.write_text(
        "MUTATION-PROOF\n"
        "ref: 0000000000000000000000000000000000000000\n"
        "paths: README.md\n"
        "run: true\n"
        "expect: whatever\n",
        encoding="utf-8",
    )
    try:
        rc = mrg.main([str(scratch)])
    finally:
        scratch.unlink()
    out = capsys.readouterr().out
    assert rc == 2, f"ожидался код 2 (только unknown, без violation), получено {rc}: {out}"
    assert "не проверено" in out
    assert "0 блок(ов) проверено, 0 расхождений" not in out
    assert "1 блок(ов) проверено, 0 расхождений" not in out


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
        # scripts/orchestra/test_scheduler.py ВЫПАЛ из корпуса прозы
        # 2026-09-14 (#1262, PR #1268): его гвардия
        # test_after_merge_never_calls_create_pool_issue_for_review_findings
        # несёт НАСТОЯЩИЙ блок MUTATION-PROOF (исполняется второй половиной
        # этой же гвардии — git grep-сканом по дереву). Держать файл в этой
        # фикстуре значило бы требовать «0 блоков» там, где блок легален.
        # Вернуть файл сюда можно только вместе с удалением того блока.
        "scripts/lib/test_check_result_migrations.py",
        "scripts/orchestra/test_stale_blocked_guard.py",
        "scripts/lib/test_provider_quota_state_guard.py",
        # scripts/orchestra/test_repo_invariants.py ВЫПАЛ из корпуса прозы
        # 2026-09-18 (#1261, PR #1263): его поведенческий тест дедупа
        # эскалации 16 (test_run_escalations_wip_gate_dedupes_repeated_ticks_
        # of_same_state) несёт НАСТОЯЩИЙ блок MUTATION-PROOF — исполняется
        # второй половиной этой же гвардии (git grep-сканом по дереву ниже,
        # шаг mutation-recipe-execution-guard.sh): возврат волатильного
        # ключа `marker_at:claimed:actual` в run_escalations красит тест
        # ровно с «1 failed». Держать файл в этой фикстуре значило бы
        # требовать «0 блоков» там, где блок легален. Вернуть файл сюда
        # можно только вместе с удалением того блока.
        "dsh-edge/test/ingest-resident-safety.test.mjs",
    ]
    existing = [REPO_ROOT / f for f in candidate_files if (REPO_ROOT / f).is_file()]
    assert len(existing) >= 5, "фикстура протухла — ни один из ожидаемых файлов не найден на диске"
    results = mrg.scan_and_verify(existing, repo_root=REPO_ROOT)
    assert results == [], (
        f"ожидалось 0 блоков MUTATION-PROOF в корпусе прозаичных рецептов, найдено {len(results)}")
