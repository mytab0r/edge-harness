#!/usr/bin/env python3
"""Тесты additive_conflict_merge.py (issue #1032).

Единичные тесты ниже строят маркеры конфликта РУКАМИ — это не «пересказ»
(AGENTS.md, «тест кормит прод-форму данных»): формат маркеров `<<<<<<< /
======= / >>>>>>>` — не внешний API с недокументированной формой ответа, а
литеральный, фиксированный синтаксис самого git (`git config
merge.conflictstyle` в этом репозитории не переопределён — обычный 2-way
вид). Живые случаи (PR #494/#542 — безопасная аддитивная вставка;
PR #629 — опасная правка одной функции с двух сторон; PR #883 — опасное
совпадение имени `ESCALATING_INVARIANTS`; PR #567 — опасное совпадение ключа
строки таблицы `ci-failure`) разобраны в докстринге модуля и цитируются в
фикстурах ниже с указанием, какой реальный PR они моделируют.

Финальный тест (test_try_resolve_end_to_end_on_a_real_git_rebase_conflict)
не строит текст руками вовсе — конфликт получен НАСТОЯЩИМ `git rebase` над
временным bare-репозиторием (тот же приём, что test_mechanical_rebase.py),
это и есть прод-форма.

Запуск: python -m pytest scripts/orchestra/test_additive_conflict_merge.py -q
"""

import subprocess
import sys
from pathlib import Path

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))

import additive_conflict_merge as acm  # noqa: E402


# ── parse_conflict_hunks ──────────────────────────────────────────────────


def test_parse_conflict_hunks_finds_single_hunk():
    text = "before\n<<<<<<< HEAD\nours line\n=======\ntheirs line\n>>>>>>> branch\nafter\n"

    hunks = acm.parse_conflict_hunks(text)

    assert len(hunks) == 1
    start, end, ours, theirs = hunks[0]
    assert ours == "ours line"
    assert theirs == "theirs line"
    lines = text.split("\n")
    assert lines[start] == "<<<<<<< HEAD"
    assert lines[end] == ">>>>>>> branch"


def test_parse_conflict_hunks_finds_multiple_hunks():
    text = (
        "a\n<<<<<<< HEAD\no1\n=======\nt1\n>>>>>>> b\n"
        "middle\n"
        "<<<<<<< HEAD\no2\n=======\nt2\n>>>>>>> b\nz\n"
    )

    hunks = acm.parse_conflict_hunks(text)

    assert [(h[2], h[3]) for h in hunks] == [("o1", "t1"), ("o2", "t2")]


def test_parse_conflict_hunks_returns_empty_list_without_markers():
    assert acm.parse_conflict_hunks("plain text\nno conflict here\n") == []


# ── Python: критерий безопасности ────────────────────────────────────────


def test_python_hunk_safe_when_both_sides_add_disjoint_top_level_statements():
    """Модель живого случая PR #494/#542 (scheduler.py): main добавила
    TASK_REPLACEMENT_MARKER (#543), PR #542 — REOPEN_ESCALATION_THRESHOLD
    (#494), в ту же точку файла — оба имени НЕ пересекаются."""
    ours = 'TASK_REPLACEMENT_MARKER = "<!-- task-replacement:auto -->"'
    theirs = "REOPEN_ESCALATION_THRESHOLD = 2"

    assert acm._python_hunk_unsafe_reason(ours, theirs) is None


def test_python_hunk_unsafe_when_both_sides_define_the_same_name():
    """Модель живого случая PR #883 (repo_invariants.py): обе стороны
    определяют `ESCALATING_INVARIANTS` с РАЗНЫМ значением — конкатенация
    оставила бы работать только последнее присваивание, молча потеряв
    первое."""
    ours = "ESCALATING_INVARIANTS = (1, 3, 12, 15, 16)"
    theirs = "ESCALATING_INVARIANTS = (1, 3, 12, 15)"

    reason = acm._python_hunk_unsafe_reason(ours, theirs)

    assert reason is not None
    assert "ESCALATING_INVARIANTS" in reason


def test_python_hunk_unsafe_when_a_side_is_an_unfinished_def():
    """Модель живого случая PR #629 (scheduler.py): обе стороны конфликта
    заканчиваются НЕЗАВЕРШЁННОЙ строкой `def append_session_notes(...):` с
    разной сигнатурой — правка ОДНОЙ функции, не два независимых
    добавления. Фрагмент без тела не разбирается ast.parse'ом сам по себе."""
    ours = (
        "_SESSION_NOTE_SEQ = itertools.count()\n\n\n"
        "def append_session_notes(notes: list[tuple[int, str]]) -> tuple[list[str], bool]:"
    )
    theirs = (
        'SESSION_NOTE_LOSS_MARKER = "[session-notes: доставка сломана]"\n\n\n'
        "def append_session_notes(repo: str, notes: list[tuple[int, str]]) -> list[str]:"
    )

    reason = acm._python_hunk_unsafe_reason(ours, theirs)

    assert reason is not None
    assert "самостоятельный" in reason


def test_python_hunk_unsafe_when_a_side_starts_with_indentation():
    """Модель второго хунка PR #883 (правка внутри функции build_report,
    строка `if findings.get(16):` с отступом) — правка ВНУТРИ существующего
    блока, не добавление на уровне модуля, ловится ДО ast.parse."""
    ours = "    if findings.get(16):\n        pass"
    theirs = "NEW_TOP_LEVEL = 1"

    reason = acm._python_hunk_unsafe_reason(ours, theirs)

    assert reason is not None
    assert "отступа" in reason


def test_hunk_unsafe_reason_rejects_empty_side():
    assert acm._hunk_unsafe_reason("X = 1", "   \n", ".py") is not None
    assert acm._hunk_unsafe_reason("", "Y = 2", ".py") is not None


def test_hunk_unsafe_reason_rejects_unsupported_suffix():
    reason = acm._hunk_unsafe_reason("a", "b", ".txt")
    assert reason is not None
    assert ".txt" in reason


# ── Markdown: критерий безопасности (docs/agents/LABELS.md) ──────────────


def test_markdown_hunk_safe_when_both_sides_add_distinct_table_rows():
    """Модель живого случая новой строки таблицы (класс PR #241/#607) —
    ключи (`quota-breach` vs `dependabot-alert`) не пересекаются."""
    ours = "| `dependabot-alert` | ... | ... | ... | ... |"
    theirs = "| `quota-breach` | ... | ... | ... | ... |"

    assert acm._markdown_hunk_unsafe_reason(ours, theirs) is None


def test_markdown_hunk_unsafe_when_both_sides_repeat_the_same_row_key():
    """Модель живого случая PR #567 (docs/agents/LABELS.md): main и PR несут
    РАЗНЫЕ версии одной и той же строки `ci-failure` — правка одной записи,
    не добавление новой."""
    ours = "| `ci-failure` | новая, более длинная версия дедупа | ... |"
    theirs = "| `ci-failure` | старая, короткая версия | ... |"

    reason = acm._markdown_hunk_unsafe_reason(ours, theirs)

    assert reason is not None
    assert "ci-failure" in reason


def test_markdown_hunk_unsafe_when_a_side_is_not_a_table_row():
    reason = acm._markdown_hunk_unsafe_reason("| `a` | b |", "просто текст без таблицы")
    assert reason is not None


# ── resolve_file_text: весь файл целиком ─────────────────────────────────


def test_resolve_file_text_concatenates_all_safe_hunks_in_order():
    text = (
        "before\n"
        "<<<<<<< HEAD\n"
        'TASK_REPLACEMENT_MARKER = "x"\n'
        "=======\n"
        "REOPEN_ESCALATION_THRESHOLD = 2\n"
        ">>>>>>> branch\n"
        "after\n"
    )

    resolved, reason = acm.resolve_file_text(text, ".py")

    assert reason == ""
    assert resolved == (
        "before\n"
        'TASK_REPLACEMENT_MARKER = "x"\n'
        "REOPEN_ESCALATION_THRESHOLD = 2\n"
        "after\n"
    )


def test_resolve_file_text_refuses_whole_file_if_any_hunk_unsafe():
    """Один безопасный хунк + один опасный (совпадение имён) — файл ЦЕЛИКОМ
    отказан, безопасный хунк тоже не применяется (частичного решения нет)."""
    text = (
        "<<<<<<< HEAD\n"
        "SAFE_A = 1\n"
        "=======\n"
        "SAFE_B = 2\n"
        ">>>>>>> branch\n"
        "middle\n"
        "<<<<<<< HEAD\n"
        "COLLIDES = 1\n"
        "=======\n"
        "COLLIDES = 2\n"
        ">>>>>>> branch\n"
    )

    resolved, reason = acm.resolve_file_text(text, ".py")

    assert resolved is None
    assert "COLLIDES" in reason


def test_resolve_file_text_reports_no_markers_found():
    resolved, reason = acm.resolve_file_text("no markers here\n", ".py")
    assert resolved is None
    assert "не найдены" in reason


# ── try_resolve: файлы на диске + верификация ────────────────────────────


def _write_conflicted_py(path: Path) -> None:
    path.write_text(
        "def existing():\n    return 1\n\n\n"
        "<<<<<<< HEAD\n"
        'TASK_REPLACEMENT_MARKER = "x"\n'
        "=======\n"
        "REOPEN_ESCALATION_THRESHOLD = 2\n"
        ">>>>>>> branch\n",
        encoding="utf-8",
    )


def test_try_resolve_writes_merged_file_when_safe_and_no_test_exists(tmp_path):
    target = tmp_path / "widget.py"
    _write_conflicted_py(target)

    result = acm.try_resolve(tmp_path, ["widget.py"])

    assert result == ["widget.py"]
    merged = target.read_text(encoding="utf-8")
    assert "TASK_REPLACEMENT_MARKER" in merged
    assert "REOPEN_ESCALATION_THRESHOLD" in merged
    assert "<<<<<<<" not in merged


def test_try_resolve_returns_none_when_unsupported_file_present(tmp_path):
    py_target = tmp_path / "widget.py"
    _write_conflicted_py(py_target)
    txt_target = tmp_path / "notes.txt"
    txt_target.write_text(
        "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n", encoding="utf-8"
    )

    result = acm.try_resolve(tmp_path, ["widget.py", "notes.txt"])

    assert result is None
    # Ни один файл не тронут (весь PR — либо всё, либо ничего).
    assert "<<<<<<<" in py_target.read_text(encoding="utf-8")


def test_try_resolve_returns_none_when_sibling_test_fails(tmp_path):
    """Верификация ДО продолжения рёбейза (задание: «результат обязан
    проверяться … при любом сомнении — отказ»): даже когда хунк формально
    безопасен, провальный pytest соседнего test_<имя>.py обязан остановить
    сведение. Файл при этом уже несёт СЛИТОЕ содержимое (не original) — это
    честный контракт модуля (см. докстринг try_resolve): откат целиком —
    дело вызывающего git rebase --abort, не этой функции."""
    target = tmp_path / "widget.py"
    _write_conflicted_py(target)
    (tmp_path / "test_widget.py").write_text(
        "def test_always_fails():\n    assert False\n", encoding="utf-8"
    )

    result = acm.try_resolve(tmp_path, ["widget.py"])

    assert result is None
    merged = target.read_text(encoding="utf-8")
    assert "<<<<<<<" not in merged  # уже слито на диске — честный контракт


def test_try_resolve_succeeds_when_sibling_test_passes(tmp_path):
    target = tmp_path / "widget.py"
    _write_conflicted_py(target)
    (tmp_path / "test_widget.py").write_text(
        "import widget\n\n\ndef test_existing_still_works():\n    assert widget.existing() == 1\n",
        encoding="utf-8",
    )

    result = acm.try_resolve(tmp_path, ["widget.py"])

    assert result == ["widget.py"]


def test_try_resolve_returns_none_when_file_missing(tmp_path):
    assert acm.try_resolve(tmp_path, ["does_not_exist.py"]) is None


# ── Прод-форма: реальный git rebase, реальный конфликт ───────────────────


def _git(*args, cwd):
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, f"git {args} упал в {cwd}: {result.stderr}"
    return result.stdout


def test_try_resolve_end_to_end_on_a_real_git_rebase_conflict(tmp_path):
    """Настоящий git rebase, настоящий каталог паузы, настоящий текст
    маркеров — не сконструированный руками. main и ветка PR независимо
    дописывают РАЗНЫЕ константы в конец одного и того же файла (тот же
    класс, что живой PR #542) — after rebase pause, try_resolve обязан
    свести это и позволить `git rebase --continue` дойти до конца."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(origin), str(seed)], check=True, capture_output=True)
    _git("config", "user.email", "test@example.com", cwd=seed)
    _git("config", "user.name", "test", cwd=seed)

    (seed / "registry.py").write_text("EXISTING = 1\n", encoding="utf-8")
    _git("add", "registry.py", cwd=seed)
    _git("commit", "-m", "base", cwd=seed)
    _git("push", "-u", "origin", "main", cwd=seed)
    base_sha = _git("rev-parse", "HEAD", cwd=seed).strip()

    _git("checkout", "-b", "agent/900-additive", base_sha, cwd=seed)
    (seed / "registry.py").write_text("EXISTING = 1\nTASK_REPLACEMENT_MARKER = 'x'\n", encoding="utf-8")
    _git("add", "registry.py", cwd=seed)
    _git("commit", "-m", "pr: add TASK_REPLACEMENT_MARKER", cwd=seed)
    _git("push", "-u", "origin", "agent/900-additive", cwd=seed)

    _git("checkout", "main", cwd=seed)
    (seed / "registry.py").write_text("EXISTING = 1\nREOPEN_ESCALATION_THRESHOLD = 2\n", encoding="utf-8")
    _git("add", "registry.py", cwd=seed)
    _git("commit", "-m", "main: add REOPEN_ESCALATION_THRESHOLD", cwd=seed)
    _git("push", "origin", "main", cwd=seed)

    work = tmp_path / "work"
    subprocess.run(["git", "clone", str(origin), str(work)], check=True, capture_output=True)
    _git("config", "user.email", "test@example.com", cwd=work)
    _git("config", "user.name", "test", cwd=work)
    _git("checkout", "-B", "agent/900-additive", "origin/agent/900-additive", cwd=work)
    result = subprocess.run(["git", "rebase", "origin/main"], cwd=work, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode != 0  # настоящий структурный конфликт

    raw = (work / "registry.py").read_text(encoding="utf-8")
    assert "<<<<<<<" in raw  # прод-форма подтверждена перед вызовом try_resolve

    resolved_paths = acm.try_resolve(work, ["registry.py"])

    assert resolved_paths == ["registry.py"]
    merged = (work / "registry.py").read_text(encoding="utf-8")
    assert "TASK_REPLACEMENT_MARKER" in merged
    assert "REOPEN_ESCALATION_THRESHOLD" in merged
    assert "<<<<<<<" not in merged

    _git("add", "registry.py", cwd=work)
    continue_result = subprocess.run(
        ["git", "rebase", "--continue"], cwd=work, capture_output=True, text=True, encoding="utf-8",
        env={**subprocess.os.environ, "GIT_EDITOR": "true"},
    )
    assert continue_result.returncode == 0, continue_result.stderr
