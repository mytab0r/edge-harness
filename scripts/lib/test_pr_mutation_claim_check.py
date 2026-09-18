#!/usr/bin/env python3
"""Тесты glue-скрипта `pr_mutation_claim_check.py` (#968) — то поведение
живой проверки, которого нет в чистой логике `mutation_claim.py`:

  - заявление «мутация доказана» проверяется ВСЕГДА, когда блок присутствует
    в теле PR, а не только когда PR меняет каталог гвардий (блокирующая
    находка ai-review PR #1028: иначе мотивирующий случай #893 — ложное
    «доказательство мутацией» в PR, трогавшем только `scripts/git/` и
    `scripts/lib/`, — проходил мимо собственно механизма молча);
  - обязательность блока остаётся только за PR, меняющими
    `scripts/ci/guards/*.sh`;
  - удаление гвардии каталога не молчит;
  - дерево, оставшееся мутированным (tree_restored=False), останавливает
    цикл заявлений — продолжение дало бы вердикты по чужой причине.

Запуск: python -m pytest scripts/lib/test_pr_mutation_claim_check.py -q
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
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

_PR_CHECK_SPEC = importlib.util.spec_from_file_location(
    "pr_mutation_claim_check", Path(__file__).resolve().parent / "pr_mutation_claim_check.py")
pr_check = importlib.util.module_from_spec(_PR_CHECK_SPEC)
_sys.modules["pr_mutation_claim_check"] = pr_check  # тот же приём регистрации до exec_module
_PR_CHECK_SPEC.loader.exec_module(pr_check)  # type: ignore[union-attr]


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


_TARGET_FIXED = 'VALUE = "fixed"\n'


def _toggle_repo(tmp_path: Path) -> Path:
    """Минимальный репозиторий: проверяемый тест + посторонний файл (для
    патча, который НИЧЕГО проверяемое не трогает — ложное заявление)."""
    (tmp_path / "target.py").write_text(_TARGET_FIXED, encoding="utf-8")
    (tmp_path / "other.py").write_text('VALUE = "other"\n', encoding="utf-8")
    (tmp_path / "test_toggle.py").write_text(
        "import target\n\n\ndef test_toggle():\n    assert target.VALUE == 'fixed'\n",
        encoding="utf-8",
    )
    _init_repo(tmp_path)
    return tmp_path


def _diff_for(tmp_path: Path, name: str, new_text: str) -> str:
    """Реальный git diff замены содержимого файла (не строка руками — та же
    гарантия накладываемости, что у живого PR)."""
    (tmp_path / name).write_text(new_text, encoding="utf-8")
    patch = _git(tmp_path, "diff").stdout
    _git(tmp_path, "checkout", "--", name)
    return patch


def _mutation_body(patch: str) -> str:
    return (
        "## Доказательство мутацией\n"
        "Тест: `python -m pytest test_toggle.py -q`\n"
        "```diff\n"
        f"{patch}"
        "```\n"
    )


# ── проверка заявления вне зависимости от файлов PR ──────────────────────────

def test_claim_present_on_non_guard_pr_is_checked(tmp_path: Path, capsys: pytest.CaptureFixture):
    """Блок заявления в PR, НЕ меняющем каталог гвардий, проверяется реальным
    прогоном, а не пропускается (блокирующая находка ai-review PR #1028):
    в выводе видны фазы доказательства и вердикт proved."""
    repo = _toggle_repo(tmp_path)
    patch = _diff_for(repo, "target.py", 'VALUE = "mutated"\n')
    failed = pr_check._run_mutation_proof(_mutation_body(patch), ["README.md"], [], repo)
    out = capsys.readouterr().out
    assert failed is False
    assert "фаза baseline (до мутации)" in out
    assert "mutation-claim[proved]" in out


def test_false_claim_on_non_guard_pr_fails_check(tmp_path: Path, capsys: pytest.CaptureFixture):
    """Ложное заявление в PR без гвардий красит проверку (мотивирующий случай
    #893: коммит 508e6899 трогал только scripts/git/ и scripts/lib/)."""
    repo = _toggle_repo(tmp_path)
    patch = _diff_for(repo, "other.py", 'VALUE = "changed"\n')  # тест это не читает
    failed = pr_check._run_mutation_proof(_mutation_body(patch), ["README.md"], [], repo)
    out = capsys.readouterr().out
    assert failed is True
    assert "mutation-claim[false_claim]" in out
    assert pr_check.GAS_NOTE in out


def test_guard_pr_without_block_is_error(tmp_path: Path, capsys: pytest.CaptureFixture):
    failed = pr_check._run_mutation_proof(
        "Тело без блоков.", ["scripts/ci/guards/foo-guard.sh"], [], tmp_path)
    out = capsys.readouterr().out
    assert failed is True
    assert "обязательно" in out
    assert pr_check.GAS_NOTE in out


def test_non_guard_pr_without_block_passes_quietly(tmp_path: Path, capsys: pytest.CaptureFixture):
    failed = pr_check._run_mutation_proof("Тело без блоков.", ["README.md"], [], tmp_path)
    out = capsys.readouterr().out
    assert failed is False
    assert "не требуется" in out


# ── удаление гвардии не молчит ────────────────────────────────────────────────

def test_removed_guard_is_reported(tmp_path: Path, capsys: pytest.CaptureFixture):
    """PR, целиком удаляющий гвардию каталога, печатает отдельную строку —
    крайнее значение объекта видно в логе (находка ai-review PR #1028)."""
    failed = pr_check._run_mutation_proof(
        "Тело без блоков.", [], ["scripts/ci/guards/foo-guard.sh"], tmp_path)
    out = capsys.readouterr().out
    assert failed is False
    assert "УДАЛЯЕТ гвардию(и) каталога: ['scripts/ci/guards/foo-guard.sh']" in out


def test_list_pr_changed_and_removed_files_returns_changed_and_removed(monkeypatch: pytest.MonkeyPatch):
    """`removed` больше не выбрасывается молча — различается по статусу
    записи, а не теряется в фильтре."""
    pages = iter([
        [
            {"filename": "a.py", "status": "modified"},
            {"filename": "scripts/ci/guards/old-guard.sh", "status": "removed"},
        ],
        [],  # вторая страница пуста → конец (класс #308, постранично)
    ])
    monkeypatch.setattr(pr_check, "_gh_api", lambda *args: next(pages))
    changed, removed = pr_check.list_pr_changed_and_removed_files("owner/repo", 1)
    assert changed == ["a.py"]
    assert removed == ["scripts/ci/guards/old-guard.sh"]


# ── отравленное дерево останавливает цикл ─────────────────────────────────────

_TWO_CLAIM_BODY = (
    "## Доказательство мутацией\n"
    "Тест: `python -m pytest test_toggle.py -q`\n"
    "```diff\n"
    "--- a/x.py\n"
    "+++ b/x.py\n"
    "```\n"
    "\n"
    "## Доказательство мутацией\n"
    "Тест: `python -m pytest test_toggle.py::test_other -q`\n"
    "```diff\n"
    "--- a/y.py\n"
    "+++ b/y.py\n"
    "```\n"
)


def test_poisoned_tree_stops_claim_loop(tmp_path: Path, capsys: pytest.CaptureFixture,
                                        monkeypatch: pytest.MonkeyPatch):
    """После заявления с tree_restored=False цикл НЕ продолжает: прогон по
    мутированному дереву дал бы следующему заявлению вердикт по чужой
    причине (блокирующая находка ai-review PR #1028)."""
    repo = _toggle_repo(tmp_path)
    # Через pr_check.mutation_claim — ЭТОТ экземпляр модуля использует glue
    # (повторная загрузка файла под тем же именем создаёт отдельный объект).
    mc = pr_check.mutation_claim
    outcomes = iter([
        mc.MutationProofOutcome("setup_error", "дерево могло остаться мутированным", tree_restored=False),
        mc.MutationProofOutcome("proved", "это заявление проверяться НЕ должно"),
    ])
    seen: list[mc.MutationClaim] = []

    def fake_run(repo_root: Path, claim: mc.MutationClaim) -> mc.MutationProofOutcome:
        seen.append(claim)
        return next(outcomes)

    monkeypatch.setattr(mc, "run_mutation_proof", fake_run)
    failed = pr_check._run_mutation_proof(_TWO_CLAIM_BODY, ["README.md"], [], repo)
    out = capsys.readouterr().out
    assert failed is True
    assert len(seen) == 1, "второе заявление проверялось на отравленном дереве"
    assert "по чужой причине" in out


def test_restored_tree_does_not_stop_claim_loop(tmp_path: Path, capsys: pytest.CaptureFixture,
                                                monkeypatch: pytest.MonkeyPatch):
    """Обратный случай: setup_error с НЕ отравленным деревом (tree_restored
    не False) остальные заявления НЕ отменяет — дерево-то чистое."""
    repo = _toggle_repo(tmp_path)
    mc = pr_check.mutation_claim
    outcomes = iter([
        mc.MutationProofOutcome("setup_error", "патч не накладывается", tree_restored=None),
        mc.MutationProofOutcome("proved", "второе проверено"),
    ])
    seen: list[mc.MutationClaim] = []

    def fake_run(repo_root: Path, claim: mc.MutationClaim) -> mc.MutationProofOutcome:
        seen.append(claim)
        return next(outcomes)

    monkeypatch.setattr(mc, "run_mutation_proof", fake_run)
    failed = pr_check._run_mutation_proof(_TWO_CLAIM_BODY, ["README.md"], [], repo)
    out = capsys.readouterr().out
    assert failed is True
    assert len(seen) == 2, "второе заявление не проверялось, хотя дерево чистое"
    assert "по чужой причине" not in out


# ── битый ERE: громкий отказ с газом, не трейсбек ────────────────────────────

def test_broken_ere_is_error_with_gas_not_traceback(tmp_path: Path, capsys: pytest.CaptureFixture):
    """`git grep` с синтаксически битым ERE (rc>=2) — ошибка ФОРМЫ заявления:
    glue печатает `::error::` с GAS_NOTE и НЕ падает трейсбеком (прежнее
    поведение: необработанный RuntimeError убивал прогон ДО мутационной фазы,
    единственный путь отказа без газа — находка ai-review PR #1028,
    чеклист)."""
    (tmp_path / "seed.py").write_text("seed\n", encoding="utf-8")
    _init_repo(tmp_path)  # живой репозиторий: ошибка именно в паттерне, не в «не git»
    body = (
        "## Класс закрыт\n"
        "Grep: `([unclosed`\n"
        "Ожидается совпадений: 0\n"
    )
    failed = pr_check._run_class_closed(body, tmp_path)
    out = capsys.readouterr().out
    assert failed is True
    assert "::error::" in out
    assert "не исполняется" in out
    assert pr_check.GAS_NOTE in out
    assert "Traceback" not in out


def test_broken_ere_does_not_stop_later_claims(tmp_path: Path, capsys: pytest.CaptureFixture):
    """Битый ERE в одном заявлении не топит остальные: второе (валидное)
    заявление всё равно проверяется и его исход печатается."""
    (tmp_path / "seed.py").write_text("old form here\n", encoding="utf-8")
    _init_repo(tmp_path)
    body = (
        "## Класс закрыт\n"
        "Grep: `([unclosed`\n"
        "Ожидается совпадений: 0\n"
        "\n"
        "## Класс закрыт\n"
        "Grep: `old form here`\n"
        "Ожидается совпадений: 1\n"
    )
    failed = pr_check._run_class_closed(body, tmp_path)
    out = capsys.readouterr().out
    assert failed is True
    assert "не исполняется" in out
    assert "подтверждено — паттерн «old form here»" in out


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"] + sys.argv[1:]))
