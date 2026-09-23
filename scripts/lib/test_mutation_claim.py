#!/usr/bin/env python3
"""Тесты `mutation_claim.py` (#968) — доказательство того, что механизм
проверки заявлений сам не является структурной гвардией того же класса,
что он ловит у других (AGENTS.md, «Доказывай доказыватель»).

Главный тест этого файла — `test_run_mutation_proof_false_claim_on_pr893_fixture`:
он кормит `run_mutation_proof` РЕАЛЬНЫМ историческим содержимым коммита
508e6899 (первый коммит PR #891/#893, ДО переделки на поведенческие тесты,
см. `fixtures_pr893_worktree_cleanup.py.txt`/`fixtures_pr893_test_worktree_cleanup_guard.py.txt`
рядом — дословные копии, не пересказ, AGENTS.md «Тест кормит прод-форму
данных»). Тест воспроизводит ровно ту мутацию, которая тогда прошла бы
незамеченной («имя есть, тела нет»), и проверяет, что `run_mutation_proof`
называет заявление ложным (`verdict == "false_claim"`), а не зелёным.

Доказательство мутацией САМОГО механизма (ручной прогон, дословный вывод —
в теле PR/отчёте, не здесь): временно убрать ветку `if mutated.passed: return
MutationProofOutcome("false_claim", ...)` в `run_mutation_proof`
(scripts/lib/mutation_claim.py) — красит именно
`test_run_mutation_proof_false_claim_on_pr893_fixture` (ассерт ждёт
`"false_claim"`, код без этой ветки после мутации возвращает `"proved"`);
вернуть ветку — GREEN.

Запуск: python -m pytest scripts/lib/test_mutation_claim.py -q
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
from pathlib import Path as _Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", _Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import sys as _sys

_MC_SPEC = importlib.util.spec_from_file_location(
    "mutation_claim", Path(__file__).resolve().parent / "mutation_claim.py")
mutation_claim = importlib.util.module_from_spec(_MC_SPEC)
_sys.modules["mutation_claim"] = mutation_claim  # @dataclass + отложенные аннотации, см. mutation_claim.py
_MC_SPEC.loader.exec_module(mutation_claim)  # type: ignore[union-attr]

REPO_ROOT = mutation_claim.REPO_ROOT
LIB_DIR = Path(__file__).resolve().parent


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {cwd}: {result.stdout}\n{result.stderr}")
    return result


def _init_repo(path: Path) -> None:
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")


# ── parse_mutation_claims ────────────────────────────────────────────────────

VALID_BODY = """\
Текст описания PR.

## Доказательство мутацией
Тест: `python -m pytest scripts/lib/test_x.py::test_y -q`
```diff
--- a/scripts/lib/x.py
+++ b/scripts/lib/x.py
@@ -1,1 +1,1 @@
-old
+new
```

## Чеклист ревью
- [ ] что-то ещё
"""


def test_parse_mutation_claims_valid_single_block():
    claims = mutation_claim.parse_mutation_claims(VALID_BODY)
    assert len(claims) == 1
    claim = claims[0]
    assert claim.test_cmd == "python -m pytest scripts/lib/test_x.py::test_y -q"
    assert claim.test_target == "scripts/lib/test_x.py::test_y"
    assert "-old" in claim.patch_text and "+new" in claim.patch_text


def test_parse_mutation_claims_no_heading_returns_empty():
    assert mutation_claim.parse_mutation_claims("Обычное тело PR без блоков.") == []


def test_parse_mutation_claims_missing_test_line_raises():
    body = "## Доказательство мутацией\n```diff\n-a\n+b\n```\n"
    with pytest.raises(mutation_claim.MutationClaimFormatError, match="Тест:"):
        mutation_claim.parse_mutation_claims(body)


def test_parse_mutation_claims_bad_command_form_raises():
    body = (
        "## Доказательство мутацией\n"
        "Тест: `pytest scripts/lib/test_x.py && rm -rf /`\n"
        "```diff\n-a\n+b\n```\n"
    )
    with pytest.raises(mutation_claim.MutationClaimFormatError, match="не разрешённой формы"):
        mutation_claim.parse_mutation_claims(body)


def test_parse_mutation_claims_missing_diff_fence_raises():
    body = (
        "## Доказательство мутацией\n"
        "Тест: `python -m pytest scripts/lib/test_x.py -q`\n"
    )
    with pytest.raises(mutation_claim.MutationClaimFormatError, match="блок"):
        mutation_claim.parse_mutation_claims(body)


def test_parse_mutation_claims_takes_diff_fence_not_first_foreign_fence():
    """Носитель патча — блок ровно ```diff: чужой fence (цитата вывода,
    пример кода) раньше диффа не подменяет патч (некритичная находка
    ai-review PR #1028 в чеклисте тела: парсер брал первый fence любым
    маркером, и автор получал «патч не накладывается» на визуально
    корректный дифф дальше по секции)."""
    body = (
        "## Доказательство мутацией\n"
        "Тест: `python -m pytest scripts/lib/test_x.py -q`\n"
        "Дословный вывод прошлого прогона:\n"
        "```text\n"
        "1 passed in 0.01s\n"
        "```\n"
        "```diff\n"
        "--- a/scripts/lib/x.py\n"
        "+++ b/scripts/lib/x.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-old\n"
        "+new\n"
        "```\n"
    )
    claims = mutation_claim.parse_mutation_claims(body)
    assert len(claims) == 1
    assert "1 passed in 0.01s" not in claims[0].patch_text
    assert "-old" in claims[0].patch_text and "+new" in claims[0].patch_text


def test_parse_mutation_claims_only_non_diff_fence_gets_targeted_error():
    """Секция с блоком ```…``` чужого маркера и без ```diff — адресная
    ошибка про носитель патча, а не общее «не найден блок» без указания,
    что именно не так с маркером."""
    body = (
        "## Доказательство мутацией\n"
        "Тест: `python -m pytest scripts/lib/test_x.py -q`\n"
        "```\n"
        "--- a/scripts/lib/x.py\n"
        "+++ b/scripts/lib/x.py\n"
        "```\n"
    )
    with pytest.raises(mutation_claim.MutationClaimFormatError, match="ровно"):
        mutation_claim.parse_mutation_claims(body)


def test_parse_mutation_claims_accepts_parameterized_test_id():
    """Параметризованный pytest-id (`test_x[param-1,2]`) проходит — находка
    ai-review PR #1028 в чеклисте: прежний класс символов отвергал `[]` и
    `,`, автор был бы вынужден целиться в весь файл вместо точечного кейса."""
    body = (
        "## Доказательство мутацией\n"
        "Тест: `python -m pytest scripts/lib/test_x.py::test_y[param-1,2] -q`\n"
        "```diff\n-a\n+b\n```\n"
    )
    claims = mutation_claim.parse_mutation_claims(body)
    assert claims[0].test_target == "scripts/lib/test_x.py::test_y[param-1,2]"


def test_parse_mutation_claims_mutation_proof_block_gets_targeted_error():
    """Секция заявления с узнаваемой формой MUTATION-PROOF (ADR 0023) даёт
    адресную ошибку про разведение поверхностей, а не вводящее в заблуждение
    «не найдена строка Тест:» (блокирующая находка ai-review PR #1028:
    автор, идущий по документации про ADR 0023, получал бы красный CI на
    формально правильном артефакте)."""
    body = (
        "## Доказательство мутацией\n"
        "# MUTATION-PROOF\n"
        "# ref: d239e324~1\n"
        "# paths: scripts/lib/x.py\n"
        "# run: python -m pytest scripts/lib/test_x.py -q\n"
        "# expect: 1 failed\n"
    )
    with pytest.raises(mutation_claim.MutationClaimFormatError, match="MUTATION-PROOF"):
        mutation_claim.parse_mutation_claims(body)


def test_parse_mutation_claims_prose_mention_of_mutation_proof_not_flagged():
    """Прозовое УПОМИНАНИЕ формата (без формы блока: маркер не на своей
    строке-комментарии, полей ref/paths/run/expect нет) не превращается в
    адресную ошибку — секция разбирается обычным путём."""
    body = (
        "## Доказательство мутацией\n"
        "Тут не нужен MUTATION-PROOF, потому что мутация гипотетическая.\n"
        "Тест: `python -m pytest scripts/lib/test_x.py -q`\n"
        "```diff\n-a\n+b\n```\n"
    )
    claims = mutation_claim.parse_mutation_claims(body)
    assert len(claims) == 1


# ── parse_class_closed_claims ────────────────────────────────────────────────

def test_parse_class_closed_claims_valid():
    body = (
        "## Класс закрыт\n"
        "Grep: `check_dirty\\(.*\\) in content`\n"
        "Ожидается совпадений: 0\n"
    )
    claims = mutation_claim.parse_class_closed_claims(body)
    assert len(claims) == 1
    assert claims[0].pattern == "check_dirty\\(.*\\) in content"
    assert claims[0].expected_count == 0


def test_parse_class_closed_claims_missing_field_raises():
    body = "## Класс закрыт\nGrep: `foo`\n"
    with pytest.raises(mutation_claim.MutationClaimFormatError):
        mutation_claim.parse_class_closed_claims(body)


# ── unverifiable disclosure ───────────────────────────────────────────────────

def test_find_unverifiable_mentions_detects_known_phrase():
    body = "Проверено вручную на живом прогоне, всё хорошо."
    assert "проверено вручную" in mutation_claim.find_unverifiable_mentions(body)


def test_find_unverifiable_mentions_ignores_fenced_code():
    body = "```\nэто пример: проверено вручную внутри диффа\n```\nОстальной текст без фраз."
    assert mutation_claim.find_unverifiable_mentions(body) == []


def test_check_unverified_disclosure_marks_disclosed():
    body = (
        "Прогнал на реальной истории репозитория.\n\n"
        "## Непроверено машиной\n"
        "- «прогнал на реальной истории» — не воспроизводимо в CI, честно непроверено\n"
    )
    check = mutation_claim.check_unverified_disclosure(body)
    assert check.mentions == ["прогнал на реальной истории"]
    assert check.disclosed is True


def test_check_unverified_disclosure_flags_undisclosed():
    body = "Прогнал на реальной истории репозитория, всё сошлось."
    check = mutation_claim.check_unverified_disclosure(body)
    assert check.mentions == ["прогнал на реальной истории"]
    assert check.disclosed is False


def test_check_unverified_disclosure_empty_section_not_disclosed():
    body = "Проверено вручную.\n\n## Непроверено машиной\n\n## Чеклист\n- готово"
    check = mutation_claim.check_unverified_disclosure(body)
    assert check.disclosed is False


# ── guard_catalog_paths_changed ───────────────────────────────────────────────

def test_guard_catalog_paths_changed_top_level_only():
    changed = [
        "scripts/ci/guards/foo-guard.sh",
        "scripts/ci/guards/sub/nested-guard.sh",
        "scripts/ci/guards/README.md",
        "scripts/lib/foo.py",
    ]
    assert mutation_claim.guard_catalog_paths_changed(changed) == ["scripts/ci/guards/foo-guard.sh"]


# ── run_class_closed_check ────────────────────────────────────────────────────

def test_run_class_closed_check_counts_matches(tmp_path: Path):
    (tmp_path / "a.py").write_text("check_dirty(x) in content\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("nothing here\n", encoding="utf-8")
    _init_repo(tmp_path)

    claim_ok = mutation_claim.ClassClosedClaim(pattern=r"check_dirty\(.*\) in content", expected_count=1)
    ok, matches = mutation_claim.run_class_closed_check(tmp_path, claim_ok)
    assert ok is True
    assert len(matches) == 1

    claim_mismatch = mutation_claim.ClassClosedClaim(pattern=r"check_dirty\(.*\) in content", expected_count=0)
    ok2, matches2 = mutation_claim.run_class_closed_check(tmp_path, claim_mismatch)
    assert ok2 is False
    assert len(matches2) == 1


# ── run_mutation_proof: setup errors ──────────────────────────────────────────

_TEST_BASELINE_RED = """\
def test_always_fails():
    assert False, "заведомо красный тест"
"""

_TEST_TOGGLE = """\
import target


def test_toggle():
    assert target.VALUE == "fixed"
"""

_TARGET_FIXED = 'VALUE = "fixed"\n'
_TARGET_MUTATED = 'VALUE = "mutated"\n'


def _make_toggle_repo(tmp_path: Path, target_value: str = _TARGET_FIXED) -> Path:
    (tmp_path / "target.py").write_text(target_value, encoding="utf-8")
    (tmp_path / "test_toggle.py").write_text(_TEST_TOGGLE, encoding="utf-8")
    _init_repo(tmp_path)
    return tmp_path


def _diff_for_toggle(tmp_path: Path) -> str:
    """Реальный git diff между FIXED и MUTATED — не строка руками (та же
    гарантия, что диф применится, что у живого PR)."""
    (tmp_path / "target.py").write_text(_TARGET_MUTATED, encoding="utf-8")
    patch = _git(tmp_path, "diff").stdout
    _git(tmp_path, "checkout", "--", "target.py")
    return patch


def test_run_mutation_proof_setup_error_baseline_red(tmp_path: Path):
    (tmp_path / "test_red.py").write_text(_TEST_BASELINE_RED, encoding="utf-8")
    _init_repo(tmp_path)
    claim = mutation_claim.MutationClaim(
        test_cmd="python -m pytest test_red.py -q",
        test_target="test_red.py",
        patch_text="--- a/nothing\n+++ b/nothing\n",
    )
    outcome = mutation_claim.run_mutation_proof(tmp_path, claim)
    assert outcome.verdict == "setup_error"
    assert "красный ДО мутации" in outcome.message
    # Ошибка ДО применения патча: дерево не трогалось вовсе.
    assert outcome.tree_restored is None


def test_run_mutation_proof_setup_error_patch_does_not_apply(tmp_path: Path):
    _make_toggle_repo(tmp_path)
    bogus_patch = (
        "--- a/target.py\n+++ b/target.py\n@@ -1,1 +1,1 @@\n"
        "-этой строки точно нет в файле\n+совсем другая строка\n"
    )
    claim = mutation_claim.MutationClaim(
        test_cmd="python -m pytest test_toggle.py -q",
        test_target="test_toggle.py",
        patch_text=bogus_patch,
    )
    outcome = mutation_claim.run_mutation_proof(tmp_path, claim)
    assert outcome.verdict == "setup_error"
    assert "не накладывается" in outcome.message
    # Дерево не тронуто.
    assert outcome.tree_restored is None
    assert _git(tmp_path, "status", "--porcelain").stdout.strip() == ""


def test_run_mutation_proof_proved_on_real_behavioral_coupling(tmp_path: Path):
    """Синтетический, но НАСТОЯЩИЙ поведенческий случай: патч меняет
    значение, которое тест реально проверяет — мутация обязана покраснеть."""
    _make_toggle_repo(tmp_path)
    patch = _diff_for_toggle(tmp_path)
    claim = mutation_claim.MutationClaim(
        test_cmd="python -m pytest test_toggle.py -q",
        test_target="test_toggle.py",
        patch_text=patch,
    )
    outcome = mutation_claim.run_mutation_proof(tmp_path, claim)
    assert outcome.verdict == "proved"
    assert outcome.tree_restored is True
    assert _git(tmp_path, "status", "--porcelain").stdout.strip() == ""


_TEST_TOGGLE_VANDAL = """\
import target


def test_toggle():
    if target.VALUE != "fixed":
        # Вандализм ТОЛЬКО под мутацией: перезапись файла ломает контекст
        # патча, `git apply -R` обязан упасть — проверяем аварийное
        # восстановление дерева (находка ai-review PR #1028).
        with open("target.py", "w", encoding="utf-8") as fh:
            fh.write('VALUE = "vandalised"\\n')
    assert target.VALUE == "fixed"
"""


def test_run_mutation_proof_revert_failure_restores_tree(tmp_path: Path):
    """Неудавшийся `git apply -R` не оставляет дерево мутированным молча:
    тест-вандал перезаписывает target.py ПОД мутацией (контекст патча
    исчезает, reverse-apply падает), аварийное восстановление
    (`git checkout --` по путям патча) возвращает файл. Исход — setup_error,
    но `tree_restored is True` и дерево чистое (находка ai-review PR #1028)."""
    (tmp_path / "target.py").write_text(_TARGET_FIXED, encoding="utf-8")
    (tmp_path / "test_toggle.py").write_text(_TEST_TOGGLE_VANDAL, encoding="utf-8")
    _init_repo(tmp_path)
    patch = _diff_for_toggle(tmp_path)
    claim = mutation_claim.MutationClaim(
        test_cmd="python -m pytest test_toggle.py -q",
        test_target="test_toggle.py",
        patch_text=patch,
    )
    outcome = mutation_claim.run_mutation_proof(tmp_path, claim)
    assert outcome.verdict == "setup_error"
    assert outcome.tree_restored is True
    assert "аварийно" in outcome.message
    assert _git(tmp_path, "status", "--porcelain").stdout.strip() == ""


def test_patch_file_paths_parses_diff_headers():
    patch = (
        "diff --git a/scripts/lib/x.py b/scripts/lib/x.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/scripts/lib/x.py\n"
        "+++ b/scripts/lib/x.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-old\n"
        "+new\n"
        "diff --git a/scripts/lib/y.py b/scripts/lib/y.py\n"
        "new file mode 100644\n"
    )
    assert mutation_claim.patch_file_paths(patch) == [
        "scripts/lib/x.py",
        "scripts/lib/y.py",
    ]
    assert mutation_claim.patch_file_paths("") == []


# ── run_mutation_proof: живая история PR #893 ────────────────────────────────

def _build_pr893_repo(tmp_path: Path) -> Path:
    """Тот же слой файлов, что был в репозитории на коммите 508e6899 (ДО
    переделки PR #893 на поведенческие тесты): scripts/git/worktree-cleanup.py
    и scripts/lib/test_worktree_cleanup_guard.py — байт-в-байт копии
    (см. fixtures_pr893_*.py.txt рядом), console_utf8.py — реальный текущий файл
    репозитория (нужен только для bootstrap-импорта тестового файла)."""
    (tmp_path / "scripts" / "git").mkdir(parents=True)
    (tmp_path / "scripts" / "lib").mkdir(parents=True)
    # Расширение фикстур — `.py.txt`, не `.py` (класс #723/#583): байт-в-байт
    # копия исторического кода 508e6899 не должна сама попадать под гвардии
    # `scripts/`-питона (console_utf8-bootstrap, orphan-test) — это застывший
    # снимок ДЛЯ теста, не живая точка входа этого репозитория.
    original = (LIB_DIR / "fixtures_pr893_worktree_cleanup.py.txt").read_text(encoding="utf-8")
    test_file = (LIB_DIR / "fixtures_pr893_test_worktree_cleanup_guard.py.txt").read_text(encoding="utf-8")
    console_utf8_src = (LIB_DIR / "console_utf8.py").read_text(encoding="utf-8")
    (tmp_path / "scripts" / "git" / "worktree-cleanup.py").write_text(original, encoding="utf-8")
    (tmp_path / "scripts" / "lib" / "test_worktree_cleanup_guard.py").write_text(test_file, encoding="utf-8")
    (tmp_path / "scripts" / "lib" / "console_utf8.py").write_text(console_utf8_src, encoding="utf-8")
    _init_repo(tmp_path)
    return tmp_path


_CHECK_DIRTY_ORIGINAL = '''\
    def check_dirty(self, worktree_path: str) -> bool:
        """Проверить наличие незакоммиченных изменений"""
        output, rc = run_cmd('git status --porcelain', cwd=worktree_path)
        if rc != 0:
            return True  # Если не смогли проверить — считаем грязным
        return bool(output.strip())
'''

_CHECK_DIRTY_GUTTED = '''\
    def check_dirty(self, worktree_path: str) -> bool:
        """Проверить наличие незакоммиченных изменений"""
        return False  # мутация #968: тело снято, имя check_dirty сохранено
'''


def _pr893_gutting_patch(tmp_path: Path) -> str:
    """Реальный git diff, снимающий ТЕЛО check_dirty при сохранении имени
    функции — ровно класс, который структурная гвардия PR #893 не видела."""
    path = tmp_path / "scripts" / "git" / "worktree-cleanup.py"
    original = path.read_text(encoding="utf-8")
    assert _CHECK_DIRTY_ORIGINAL in original, "фикстура разошлась с ожидаемым текстом check_dirty"
    mutated = original.replace(_CHECK_DIRTY_ORIGINAL, _CHECK_DIRTY_GUTTED)
    path.write_text(mutated, encoding="utf-8")
    patch = _git(tmp_path, "diff").stdout
    _git(tmp_path, "checkout", "--", "scripts/git/worktree-cleanup.py")
    return patch


def test_run_mutation_proof_false_claim_on_pr893_fixture(tmp_path: Path):
    """ГЛАВНЫЙ тест: механизм обязан поймать ровно ту ложную мутацию, что
    прошла бы структурную гвардию PR #893 (см. докстринг модуля)."""
    repo = _build_pr893_repo(tmp_path)
    patch = _pr893_gutting_patch(repo)
    claim = mutation_claim.MutationClaim(
        test_cmd=(
            "python -m pytest scripts/lib/test_worktree_cleanup_guard.py"
            "::test_script_has_safety_checks -q"
        ),
        test_target="scripts/lib/test_worktree_cleanup_guard.py::test_script_has_safety_checks",
        patch_text=patch,
    )
    outcome = mutation_claim.run_mutation_proof(repo, claim)
    assert outcome.verdict == "false_claim", outcome.report()
    assert outcome.tree_restored is True
    # Рабочее дерево обязано вернуться в исходное состояние независимо от вердикта.
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""


# ── _sections: fenced-блоки не терминируют секцию ────────────────────────────

def test_sections_hash_context_line_inside_diff_fence_not_truncated():
    """Строка « ## Раздел» ВНУТРИ дифф-фенса (заголовок markdown в диффе,
    после strip начинающаяся с «## ») не терминирует секцию заявления —
    раньше секция обрезалась на середине патча, и автор видел отказ
    «не найден блок ```diff» на визуально корректном блоке (находка
    ai-review PR #1028)."""
    body = (
        "## Доказательство мутацией\n"
        "Тест: `python -m pytest scripts/lib/test_x.py::test_y -q`\n"
        "```diff\n"
        "--- a/docs/readme.md\n"
        "+++ b/docs/readme.md\n"
        "@@ -1,2 +1,2 @@\n"
        " ## Раздел\n"
        "-старое\n"
        "+новое\n"
        "```\n"
        "\n"
        "## Чеклист ревью\n"
        "- [ ] пункт\n"
    )
    sections = mutation_claim._sections(body, mutation_claim.MUTATION_HEADING)
    assert len(sections) == 1
    assert " ## Раздел" in sections[0]
    assert "+новое" in sections[0]
    # И полный parse на таком теле проходит, а не падает «не найден блок».
    claims = mutation_claim.parse_mutation_claims(body)
    assert len(claims) == 1
    assert "+новое" in claims[0].patch_text


def test_sections_heading_inside_fence_does_not_start_section():
    """Заголовок внутри фенса (цитата формата блока в примере) не открывает
    секцию — фенс-состояние учитывается и ДО секции."""
    body = (
        "Пролог.\n"
        "```\n"
        "## Доказательство мутацией\n"
        "```\n"
        "Эпилог без блоков.\n"
    )
    assert mutation_claim._sections(body, mutation_claim.MUTATION_HEADING) == []


if __name__ == "__main__":
    import sys
    raise SystemExit(pytest.main([__file__, "-q"] + sys.argv[1:]))


# ── #1436: паттерн «Класс закрыт» — ERE, а не PCRE ───────────────────────────
#
# Две ловушки, и они лечатся по-разному (замер живым `git grep` 2026-09-22,
# временный репозиторий — не документация и не пересказ):
#   громкая — git падает rc=128 текстом, который не называет ни причину, ни
#             лечение («Invalid preceding regular expression»);
#   тихая   — git НЕ падает и считает не то, а гейт «подтверждает» заявление,
#             которого автор не делал.


def test_noncapturing_group_is_named_before_git_grep_runs():
    """Громкая ловушка. Живой случай PR #1434 (прогон 35674153855): автор
    написал `(?:…)`, получил ретрансляцию текста git и пошёл править
    экранирование — то есть гадать. Цена: полный цикл CI плюс лишний коммит,
    потому что одна правка тела PR прогон не перезапускает."""
    problems = mutation_claim.ere_pattern_problems(r'if "(?:HTTP 404|session-not-found)" in str\(')

    assert len(problems) == 1
    assert "(?:" in problems[0]
    assert "ERE" in problems[0]
    assert "(…)" in problems[0], "сообщение обязано назвать замену, а не только диагноз"


def test_digit_class_is_silently_wrong_and_is_named_as_such():
    """ТИХАЯ ловушка, и она опаснее громкой. `\\d` в `git grep -nE` матчит
    БУКВУ «d», не цифру — замер: файл со строкой «5» не совпал, файл со
    строкой «d» совпал. Заявление «совпадений 3» проходит гейт и не значит
    ничего.

    Задача #1436 ставила `\\d` в один ряд с `(?:` как «тоже падает». Замер
    показал обратное — и именно поэтому проверка нужна: падения-то и нет."""
    problems = mutation_claim.ere_pattern_problems(r"worker run \d+")

    assert len(problems) == 1
    assert "НЕ падает" in problems[0]
    assert "[0-9]" in problems[0]


def test_lazy_quantifier_is_silently_greedy():
    """Вторая тихая ловушка, задачей #1436 не названная вовсе. `q+?xyz`
    совпал со строкой «xyz», где ни одного `q` нет: `?` прочитан как
    «предыдущее необязательно». `<.*?>` на «<a><b>» вернул строку целиком."""
    problems = mutation_claim.ere_pattern_problems(r"<.*?>")

    assert len(problems) == 1
    assert "ленивый" in problems[0]
    assert "[^>]" in problems[0], "сообщение обязано показать выразимую в ERE замену"


def test_gnu_extensions_that_actually_work_are_not_rejected():
    """Газ: `\\w`, `\\s`, `\\b` в `git grep -nE` работают как ожидается
    (GNU-расширения, проверено тем же замером). Отвергать их — тормоз без
    причины, и он выгнал бы автора переписывать рабочий паттерн."""
    assert mutation_claim.ere_pattern_problems(r"\bdef\s+\w+") == []


def test_escaped_metacharacters_are_not_false_positives():
    """`\\(` — литеральная скобка (в паттернах этого репозитория встречается
    постоянно), `\\*?` — литеральная звёздочка, сделанная необязательной,
    `\\\\d` — литеральный слэш плюс буква. Ни одно из трёх не PCRE-ловушка;
    различает их только чётность слэшей слева, поэтому проверка обходит
    паттерн посимвольно, а не регуляркой по нему."""
    assert mutation_claim.ere_pattern_problems(r"^def active_worker_run_for_task\(") == []
    assert mutation_claim.ere_pattern_problems(r"a\*?b") == []
    assert mutation_claim.ere_pattern_problems(r"\\d") == []


def test_claim_with_pcre_cannot_be_constructed_at_all():
    """Проверка живёт в `__post_init__`, а не в `run_class_closed_check`:
    объект с ловушкой физически не существует, и обойти её, собрав claim
    руками, нельзя. «Нарушить невозможно», а не «написано» (AGENTS.md)."""
    with pytest.raises(mutation_claim.MutationClaimFormatError) as error:
        mutation_claim.ClassClosedClaim(pattern=r"(?:a|b)", expected_count=0)

    assert "POSIX ERE" in str(error.value)


def test_body_with_pcre_pattern_fails_parsing_not_the_grep():
    """Сквозь разбор тела PR: ошибка приходит на этапе формата, рядом с
    остальными ошибками контракта, а не из недр git."""
    body = (
        "## Класс закрыт\n\n"
        "Grep: `worker run (?:\\d+)`\n"
        "Ожидается совпадений: 1\n"
    )

    with pytest.raises(mutation_claim.MutationClaimFormatError):
        mutation_claim.parse_class_closed_claims(body)


def test_valid_ere_claim_still_parses_unchanged():
    """Регрессия: честный ERE-паттерн проходит ровно как раньше."""
    body = (
        "## Класс закрыт\n\n"
        "Grep: `^def active_worker_run_for_task`\n"
        "Ожидается совпадений: 1\n"
    )

    claims = mutation_claim.parse_class_closed_claims(body)

    assert len(claims) == 1
    assert claims[0].pattern == "^def active_worker_run_for_task"
    assert claims[0].expected_count == 1
