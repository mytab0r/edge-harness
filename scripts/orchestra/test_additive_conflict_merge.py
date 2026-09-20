#!/usr/bin/env python3
"""Тесты additive_conflict_merge.py (issue #1032).

Единичные тесты ниже строят маркеры конфликта РУКАМИ — это не «пересказ»
(AGENTS.md, «тест кормит прод-форму данных»): формат маркеров `<<<<<<<`,
`=======`, `>>>>>>>` — не внешний API с недокументированной формой ответа, а
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


def test_try_resolve_refuses_duplicate_names_across_two_hunks(tmp_path):
    """Находка ai-review PR #1033, круг 3: перехунковый критерий ловит
    «обе стороны ОДНОГО хунка» (тест COLLIDES выше), но тот же класс
    проходит двумя РАЗНЫМИ хунками одного файла: ours первого и theirs
    второго определяют одно имя — каждая сторона внутри своего хунка
    независима, ast.parse итога зелёный, молча живёт последнее
    присваивание (класс #883). Мутационное доказательство: сними проверку
    _duplicate_top_level_names в try_resolve — тест краснеет (result ==
    ["dup.py"]), верни — снова зелёный."""
    target = tmp_path / "dup.py"
    target.write_text(
        "def existing():\n    return 1\n\n\n"
        "<<<<<<< HEAD\n"
        "WORKER_TIMEOUT = 30\n"
        "=======\n"
        "OTHER_A = 1\n"
        ">>>>>>> branch\n"
        "middle\n"
        "<<<<<<< HEAD\n"
        "OTHER_B = 2\n"
        "=======\n"
        "WORKER_TIMEOUT = 99\n"
        ">>>>>>> branch\n",
        encoding="utf-8",
    )

    result = acm.try_resolve(tmp_path, ["dup.py"])

    assert result is None
    # Отказ до записи: конфликтные маркеры на месте.
    assert "<<<<<<<" in target.read_text(encoding="utf-8")


def test_try_resolve_refuses_duplicate_table_keys_across_two_hunks(tmp_path):
    """Тот же класс для .md (находка ai-review PR #1033): один ключ строки
    от ours одного хунка и theirs другого — правка ОДНОЙ записи реестра, не
    два независимых добавления, как бы ни были разнесены хунки."""
    target = tmp_path / "table.md"
    target.write_text(
        "# Реестр\n\n"
        "| ключ | значение |\n"
        "|---|---|\n"
        "| a | 1 |\n"
        "<<<<<<< HEAD\n"
        "| b | 2 |\n"
        "=======\n"
        "| c | 3 |\n"
        ">>>>>>> branch\n"
        "хвост\n"
        "<<<<<<< HEAD\n"
        "| d | 4 |\n"
        "=======\n"
        "| b | 5 |\n"
        ">>>>>>> branch\n",
        encoding="utf-8",
    )

    result = acm.try_resolve(tmp_path, ["table.md"])

    assert result is None
    assert "| b |" in target.read_text(encoding="utf-8")


def test_try_resolve_allows_two_hunks_with_disjoint_names(tmp_path):
    """Позитивный контроль к проверке дубликатов: два хунка, все имена
    верхнего уровня различны — сведение проходит, ничего лишнего не
    отказано (цена ложного срабатывания — такт агентского пути, но
    перехватывать и безопасные формы критерий не должен)."""
    target = tmp_path / "ok.py"
    target.write_text(
        "def existing():\n    return 1\n\n\n"
        "<<<<<<< HEAD\n"
        "A_FROM_MAIN = 1\n"
        "=======\n"
        "B_FROM_BRANCH = 2\n"
        ">>>>>>> branch\n"
        "middle\n"
        "<<<<<<< HEAD\n"
        "C_FROM_MAIN = 3\n"
        "=======\n"
        "D_FROM_BRANCH = 4\n"
        ">>>>>>> branch\n",
        encoding="utf-8",
    )

    result = acm.try_resolve(tmp_path, ["ok.py"])

    assert result == ["ok.py"]
    merged = target.read_text(encoding="utf-8")
    assert "A_FROM_MAIN" in merged and "D_FROM_BRANCH" in merged
    assert "<<<<<<<" not in merged


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


def test_verification_pytest_does_not_see_repo_write_token(tmp_path, monkeypatch):
    """Верификационный pytest исполняет код ИЗ PR (test_*.py ветки и всё, что
    они импортируют) — под write-токеном репозитория он исполняться не должен
    (находка ai-review PR #1033).

    Проверка ПОВЕДЕНЧЕСКАЯ, не структурная: соседний тест — настоящий файл,
    который сам смотрит в своё `os.environ` и падает, увидев токен. Токены
    реально выставлены в окружении родителя, так что зелёный результат
    доказывает, что до дочернего процесса они не доехали. Структурная
    проверка «в вызове есть env=» это доказать не может: env мог бы нести
    токены.
    """
    for name in acm.VERIFICATION_STRIPPED_ENV_VARS:
        monkeypatch.setenv(name, "не-должно-доехать-до-дочернего-процесса")
    monkeypatch.setenv("ACM_HARMLESS_VAR", "должно-доехать")

    target = tmp_path / "widget.py"
    _write_conflicted_py(target)
    (tmp_path / "test_widget.py").write_text(
        "import os\n\n\n"
        "def test_no_repo_token_in_child_env():\n"
        "    leaked = [n for n in (" + ", ".join(
            repr(name) for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_PIPELINE_PAT")
        ) + ") if n in os.environ]\n"
        "    assert leaked == [], leaked\n\n\n"
        "def test_ordinary_env_survives():\n"
        "    assert os.environ.get('ACM_HARMLESS_VAR') == 'должно-доехать'\n",
        encoding="utf-8",
    )

    assert acm.try_resolve(tmp_path, ["widget.py"]) == ["widget.py"]


def test_try_resolve_refuses_non_utf8_file_instead_of_raising(tmp_path, capsys):
    """Блокирующая находка ai-review PR #1033 (круг 5): `UnicodeDecodeError` —
    наследник `ValueError`, а не `OSError`, поэтому уходил МИМО механизма
    отказа и ронял весь проход job'а (process_pull ловит только RuntimeError).

    Тест кормит ПРОД-ФОРМУ дефекта, не пересказ: настоящие байты в cp1251,
    записанные в .md-файл с настоящими маркерами конфликта, — ровно то, что
    получит модуль из рабочего дерева после `git rebase`."""
    target = tmp_path / "notes.md"
    target.write_bytes(
        "<<<<<<< HEAD\n| ключ-один | значение |\n=======\n"
        "| ключ-два | значение |\n>>>>>>> branch\n".encode("cp1251")
    )

    result = acm.try_resolve(tmp_path, ["notes.md"])

    assert result is None
    printed = capsys.readouterr().out
    assert "не читается как UTF-8" in printed, printed
    # Причина не слита с соседним случаем — их лечат по-разному.
    assert "rename/delete" not in printed


def test_try_resolve_refuses_truncated_markers_instead_of_raising(tmp_path, capsys):
    """Та же семья: `parse_conflict_hunks` поднимает ValueError на обрезанных
    маркерах («<<<<<<<» без «>>>>>>>»). Штатный отказ, не падение прохода."""
    target = tmp_path / "widget.py"
    target.write_text(
        "def existing():\n    return 1\n\n\n"
        "<<<<<<< HEAD\n"
        'TASK_REPLACEMENT_MARKER = "x"\n'
        "=======\n"
        "REOPEN_ESCALATION_THRESHOLD = 2\n",
        encoding="utf-8",
    )

    result = acm.try_resolve(tmp_path, ["widget.py"])

    assert result is None
    assert "маркеры конфликта не разбираются" in capsys.readouterr().out


def test_try_resolve_notice_names_what_was_verified(tmp_path, capsys):
    """Строка отчёта не имеет права заявлять прогон тестов, которых не было
    (находка ai-review PR #1033, круг 5). Соседних test_*.py нет — notice
    говорит это прямо, а не молчит."""
    _write_conflicted_py(tmp_path / "widget.py")

    assert acm.try_resolve(tmp_path, ["widget.py"]) == ["widget.py"]
    printed = capsys.readouterr().out
    assert "соседних test_*.py" in printed, printed
    assert "pytest не запускался" in printed


def test_try_resolve_returns_none_when_file_missing(tmp_path):
    assert acm.try_resolve(tmp_path, ["does_not_exist.py"]) is None


def test_try_resolve_prints_refusal_reason_to_job_log(tmp_path, capsys):
    """Отказ не имеет права быть невидимым в логе job'а (находка ai-review
    PR #1033, замечание из чеклиста: «отказ верификации неотличим от „не наш
    класс“ и невидим в логе» — рычаг мог бы молча мертветь в проде, как те
    самые 0/12 замера). Причина обязана прийти одной строкой с ::warning::,
    с именем файла и конкретикой отказа.

    Мутация: сделай _refuse пустышкой (return None без print) — тест краснеет
    на первом assert, при этом ВСЕ прочие тесты этого файла остаются
    зелёными: print не влияет ни на один возвращаемый результат."""
    target = tmp_path / "widget.py"
    _write_conflicted_py(target)
    colliding = tmp_path / "collides.py"
    colliding.write_text(
        "<<<<<<< HEAD\nCOLLIDES = 1\n=======\nCOLLIDES = 2\n>>>>>>> branch\n",
        encoding="utf-8",
    )

    result = acm.try_resolve(tmp_path, ["widget.py", "collides.py"])

    assert result is None
    out = capsys.readouterr().out
    assert "::warning::" in out
    assert "отказываюсь" in out
    assert "collides.py" in out          # какой файл отказан
    assert "COLLIDES" in out             # почему (совпадение имени верхнего уровня)
    warning_line = next(line for line in out.splitlines() if "::warning::" in line)
    assert "COLLIDES" in warning_line    # причина и правда в той же строке


def test_try_resolve_prints_pytest_tail_when_verification_fails(tmp_path, capsys):
    """Провал верификации обязан называть СВОЮ причину, включая хвост вывода
    pytest одной строкой — «No module named pytest», красный тест и падение
    сбора — разные причины с разными лекарствами, а не обезличенный отказ
    (тот же находка ai-review PR #1033)."""
    target = tmp_path / "widget.py"
    _write_conflicted_py(target)
    (tmp_path / "test_widget.py").write_text(
        "def test_always_fails():\n    assert False\n", encoding="utf-8"
    )

    result = acm.try_resolve(tmp_path, ["widget.py"])

    assert result is None
    warning_line = next(
        line for line in capsys.readouterr().out.splitlines() if "::warning::" in line
    )
    assert "pytest" in warning_line
    assert "rc=" in warning_line
    assert "test_always_fails" in warning_line or "failed" in warning_line


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


# ── Shell (#1383): самый частый отказ по типу файла ──────────────────────────
#
# Почему именно .sh, а не «файлы без расширения» из постановки: замер живого
# прогона conflict-mechanical-rebase 35491299405 (2026-09-20T05:17Z) —
# `.sh` ×3, `.yml` ×2, `.json` ×1, без расширения ×1. Направление выбрано по
# замеру, а не по порядку перечисления в тексте задачи.

def test_shell_hunk_safe_when_both_sides_add_disjoint_functions():
    ours = 'alpha() {\n  echo a\n}\n'
    theirs = 'beta() {\n  echo b\n}\n'
    assert acm._hunk_unsafe_reason(ours, theirs, ".sh") is None


def test_shell_hunk_unsafe_when_both_sides_define_the_same_function():
    """Тот же класс, что #883 у питона: обе стороны правят ОДНО имя, и слепая
    конкатенация оставит живым только последнее определение."""
    ours = 'drain() {\n  echo ours\n}\n'
    theirs = 'drain() {\n  echo theirs\n}\n'
    reason = acm._hunk_unsafe_reason(ours, theirs, ".sh")
    assert reason is not None and "drain" in reason


def test_shell_hunk_unsafe_when_both_sides_assign_the_same_variable():
    ours = 'TIMEOUT=30\n'
    theirs = 'TIMEOUT=99\n'
    reason = acm._hunk_unsafe_reason(ours, theirs, ".sh")
    assert reason is not None and "TIMEOUT" in reason


def test_shell_hunk_unsafe_when_a_side_is_an_unfinished_function():
    """Оборванная половина конструкции обязана отказывать: склеивать
    половинки в аддитивном слиянии нельзя. Ловит это `bash -n`, а не
    самодельный разбор."""
    ours = 'alpha() {\n  echo a\n'
    theirs = 'beta() {\n  echo b\n}\n'
    reason = acm._hunk_unsafe_reason(ours, theirs, ".sh")
    assert reason is not None and "bash -n" in reason


def test_shell_hunk_unsafe_when_a_side_starts_with_indentation():
    ours = '  echo inside\n'
    theirs = 'beta() {\n  echo b\n}\n'
    reason = acm._hunk_unsafe_reason(ours, theirs, ".sh")
    assert reason is not None and "отступа" in reason


def test_shell_local_variable_inside_function_is_not_a_top_level_name():
    """Контроль на ложный отказ: `local x=` внутри функции стоит с отступом и
    в множество верхнеуровневых имён попадать не должен — иначе две
    независимые функции с одноимённой локальной переменной давали бы отказ."""
    ours = 'alpha() {\n  local tmp=1\n  echo $tmp\n}\n'
    theirs = 'beta() {\n  local tmp=2\n  echo $tmp\n}\n'
    assert acm._hunk_unsafe_reason(ours, theirs, ".sh") is None


def test_language_of_reads_shebang_for_extensionless_file(tmp_path):
    """Живой отказ прогона 35491299405 дословно: `scripts/gh/issue-create:
    тип файла (без расширения) не поддержан`. Файл исполняемый и шелловый —
    отказывать ему по отсутствию суффикса значит судить по орфографии имени."""
    path = tmp_path / "issue-create"
    text = "#!/usr/bin/env bash\nset -euo pipefail\n"
    assert acm.language_of(path, text) == ".sh"
    assert acm.language_of(tmp_path / "tool", "#!/usr/bin/env python3\nx = 1\n") == ".py"
    assert acm.language_of(tmp_path / "data", "просто текст\n") is None
    assert acm.language_of(tmp_path / "config.yml", "a: 1\n") is None


def test_try_resolve_merges_extensionless_shell_script(tmp_path):
    """Сквозной путь того самого отказа: файл без расширения, шелл, две
    независимые функции — обязан слиться."""
    target = tmp_path / "issue-create"
    target.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n\n"
        "<<<<<<< HEAD\n"
        "alpha() {\n  echo a\n}\n"
        "=======\n"
        "beta() {\n  echo b\n}\n"
        ">>>>>>> branch\n",
        encoding="utf-8",
    )

    result = acm.try_resolve(tmp_path, ["issue-create"])

    assert result is not None, "шелл без расширения обязан сводиться (#1383)"
    merged = target.read_text(encoding="utf-8")
    assert "alpha()" in merged and "beta()" in merged
    assert "<<<<<<<" not in merged


def test_try_resolve_refuses_shell_duplicate_names_across_two_hunks(tmp_path):
    """Тот же класс «двумя РАЗНЫМИ хунками», что закрыт для .py в круге 3
    ревью PR #1033: перехунковый критерий его не видит, bash -n зелёный,
    молча живёт последнее определение."""
    target = tmp_path / "tool.sh"
    target.write_text(
        "#!/usr/bin/env bash\n\n"
        "<<<<<<< HEAD\n"
        "TIMEOUT=30\n"
        "=======\n"
        "OTHER_A=1\n"
        ">>>>>>> branch\n"
        "echo middle\n"
        "<<<<<<< HEAD\n"
        "OTHER_B=2\n"
        "=======\n"
        "TIMEOUT=99\n"
        ">>>>>>> branch\n",
        encoding="utf-8",
    )

    result = acm.try_resolve(tmp_path, ["tool.sh"])

    assert result is None
    assert "<<<<<<<" in target.read_text(encoding="utf-8"), "отказ ДО записи"


def test_try_resolve_notice_names_weaker_guarantee_for_shell(tmp_path, capsys):
    """Гарантия для шелла слабее питоновской (третьего рубежа — тестов — нет),
    и отчёт обязан говорить это вслух, а не оставлять читателя достраивать."""
    target = tmp_path / "tool.sh"
    target.write_text(
        "#!/usr/bin/env bash\n\n"
        "<<<<<<< HEAD\n"
        "alpha() {\n  echo a\n}\n"
        "=======\n"
        "beta() {\n  echo b\n}\n"
        ">>>>>>> branch\n",
        encoding="utf-8",
    )

    assert acm.try_resolve(tmp_path, ["tool.sh"]) is not None
    out = capsys.readouterr().out
    assert "bash -n" in out and "гарантия слабее" in out, out


# ── heredoc: блокирующая находка ai-ревью PR #1392 ───────────────────────────
#
# Тело heredoc — ДАННЫЕ, и все три рубежа шелла слепы на нём ОДНОВРЕМЕННО:
# строка стоит в нулевой колонке (не отступ), не определяет имён (не
# пересекаются), `bash -n` принимает произвольную прозу внутри heredoc
# (синтаксис валиден). Правка ОДНОЙ строки текста двумя сторонами выглядела
# бы как два независимых добавления. И это не теория: scripts/gh/issue-create
# — файл ИЗ ЗАМЕРА задачи — несёт heredoc'и с markdown-телами issue.

_ISSUE_CREATE_LIKE = (
    "#!/usr/bin/env bash\n"
    "set -euo pipefail\n"
    "\n"
    "body=$(cat <<'EOF_BODY'\n"
    "### Цель\n"
    "<<<<<<< HEAD\n"
    "Морда обязана отвечать за 200 мс\n"
    "=======\n"
    "Морда обязана отвечать за 500 мс\n"
    ">>>>>>> branch\n"
    "EOF_BODY\n"
    ")\n"
    'echo "$body"\n'
)


def test_shell_conflict_inside_heredoc_is_refused(tmp_path):
    """Прод-форма живого файла: конфликт ОДНОЙ строки markdown-тела внутри
    heredoc. До правки try_resolve возвращал успех и клал в файл ОБЕ
    взаимоисключающие строки — `bash -n` итогового файла при этом rc=0, и
    результат уехал бы как «сведённый»."""
    target = tmp_path / "issue-create"
    target.write_text(_ISSUE_CREATE_LIKE, encoding="utf-8")

    result = acm.try_resolve(tmp_path, ["issue-create"])

    assert result is None, "конфликт внутри heredoc сводить нельзя — это правка данных"
    text = target.read_text(encoding="utf-8")
    assert "<<<<<<<" in text, "отказ ДО записи"
    assert not ("200 мс" in text and "500 мс" in text and "<<<<<<<" not in text)


def test_shell_heredoc_refusal_names_the_delimiter(tmp_path):
    """Отказ обязан называть ограничитель, а не просто «не наш класс» —
    иначе разбирающий не поймёт, чем этот отказ отличается от прочих."""
    _, reason = acm.resolve_file_text(_ISSUE_CREATE_LIKE, ".sh")
    assert "heredoc" in reason and "EOF_BODY" in reason, reason


def test_shell_conflict_after_closed_heredoc_still_merges(tmp_path):
    """Контроль на противоположную ошибку: heredoc ЗАКРЫТ выше по файлу —
    состояние не должно «залипать», иначе рубеж отверг бы всё после первого
    же heredoc'а и выключил поддержку шелла целиком, оставаясь зелёным."""
    target = tmp_path / "tool.sh"
    target.write_text(
        "#!/usr/bin/env bash\n"
        "cat <<'EOF'\n"
        "просто текст\n"
        "EOF\n"
        "\n"
        "<<<<<<< HEAD\n"
        "alpha() {\n  echo a\n}\n"
        "=======\n"
        "beta() {\n  echo b\n}\n"
        ">>>>>>> branch\n",
        encoding="utf-8",
    )

    assert acm.try_resolve(tmp_path, ["tool.sh"]) is not None
    merged = target.read_text(encoding="utf-8")
    assert "alpha()" in merged and "beta()" in merged


def test_shell_herestring_is_not_mistaken_for_heredoc(tmp_path):
    """`<<<` — herestring, не heredoc. Спутать значит отвергать безобидные
    файлы навсегда (состояние «heredoc открыт» никогда не закроется)."""
    target = tmp_path / "tool.sh"
    target.write_text(
        "#!/usr/bin/env bash\n"
        'grep x <<< "$VAR"\n'
        "\n"
        "<<<<<<< HEAD\n"
        "alpha() {\n  echo a\n}\n"
        "=======\n"
        "beta() {\n  echo b\n}\n"
        ">>>>>>> branch\n",
        encoding="utf-8",
    )

    assert acm.try_resolve(tmp_path, ["tool.sh"]) is not None


def test_shell_names_cover_alias_and_declare_without_flag():
    """Неполный список форм определения имени означал бы «имена не
    пересеклись» там, где они пересекаются (находка ai-ревью PR #1392)."""
    for ours, theirs, expected in (
        ("alias ll='ls -l'\n", "alias ll='ls -la'\n", "ll"),
        ("declare COUNT=1\n", "declare COUNT=2\n", "COUNT"),
        ("typeset NAME=a\n", "typeset NAME=b\n", "NAME"),
    ):
        reason = acm._hunk_unsafe_reason(ours, theirs, ".sh")
        assert reason is not None and expected in reason, (ours, reason)


def test_notice_does_not_claim_ast_parse_when_no_python_in_batch(tmp_path, capsys):
    """Без .py говорить «только ast.parse итоговых файлов» — неправда:
    ast.parse тут не звался вовсе."""
    target = tmp_path / "tool.sh"
    target.write_text(
        "#!/usr/bin/env bash\n\n"
        "<<<<<<< HEAD\n"
        "alpha() {\n  echo a\n}\n"
        "=======\n"
        "beta() {\n  echo b\n}\n"
        ">>>>>>> branch\n",
        encoding="utf-8",
    )

    assert acm.try_resolve(tmp_path, ["tool.sh"]) is not None
    out = capsys.readouterr().out
    assert "только ast.parse итоговых файлов" not in out, out
    assert "питоновских файлов в пачке нет" in out, out


def test_shell_two_safe_hunks_merge_despite_earlier_conflict_marker(tmp_path):
    """Второй хунк не должен считаться «внутри heredoc» из-за строки маркера
    ПЕРВОГО хунка.

    Дыра была настоящей: регексп с одним только lookahead видел в
    `<<<<<<< HEAD` heredoc с ограничителем «HEAD», и любой второй хунк файла
    отвергался. Прежние тесты на это молчали — они ждали отказа и по другой
    причине (дубликат имени), то есть зеленели не на том."""
    target = tmp_path / "tool.sh"
    target.write_text(
        "#!/usr/bin/env bash\n\n"
        "<<<<<<< HEAD\n"
        "alpha() {\n  echo a\n}\n"
        "=======\n"
        "beta() {\n  echo b\n}\n"
        ">>>>>>> branch\n"
        "echo middle\n"
        "<<<<<<< HEAD\n"
        "gamma() {\n  echo g\n}\n"
        "=======\n"
        "delta() {\n  echo d\n}\n"
        ">>>>>>> branch\n",
        encoding="utf-8",
    )

    assert acm.try_resolve(tmp_path, ["tool.sh"]) is not None, (
        "оба хунка безопасны — файл обязан свестись")
    merged = target.read_text(encoding="utf-8")
    for name in ("alpha()", "beta()", "gamma()", "delta()"):
        assert name in merged, (name, merged)


def test_shell_bare_word_herestring_does_not_open_a_phantom_heredoc(tmp_path):
    """`<<< HELLO` без кавычек — herestring. Прежний тест брал `<<< "$VAR"`,
    который не матчился НИ ОДНИМ из регекспов, и потому не различал
    сломанный от исправного: мутация «убрать границу» оставляла его зелёным."""
    target = tmp_path / "tool.sh"
    target.write_text(
        "#!/usr/bin/env bash\n"
        "grep x <<< HELLO\n"
        "\n"
        "<<<<<<< HEAD\n"
        "alpha() {\n  echo a\n}\n"
        "=======\n"
        "beta() {\n  echo b\n}\n"
        ">>>>>>> branch\n",
        encoding="utf-8",
    )

    assert acm.try_resolve(tmp_path, ["tool.sh"]) is not None


def test_shell_side_that_opens_heredoc_is_refused(tmp_path):
    """Сцена ai-ревью PR #1392 (второй заход), воспроизведённая дословно:
    ours открывает heredoc и не закрывает, theirs определяет функцию.

    До правки проходили ВСЕ рубежи: строки до хунка чисты, отступа нет, имена
    не пересекаются, а `bash -n` на незакрытом heredoc возвращает НОЛЬ
    (печатает warning). В записанном файле `beta()` и весь хвост становились
    телом heredoc'а «A» — то есть правка одной конструкции, которую дифф
    считает двумя независимыми вставками."""
    target = tmp_path / "tool.sh"
    target.write_text(
        "#!/usr/bin/env bash\n\n"
        "<<<<<<< HEAD\n"
        "cat <<A\n"
        "BODY\n"
        "=======\n"
        "beta() {\n  echo b\n}\n"
        ">>>>>>> branch\n"
        "echo tail\n",
        encoding="utf-8",
    )

    result = acm.try_resolve(tmp_path, ["tool.sh"])

    assert result is None, "сторона, открывшая heredoc, — не самостоятельная вставка"
    assert "<<<<<<<" in target.read_text(encoding="utf-8"), "отказ ДО записи"


def test_shell_side_heredoc_refusal_names_the_delimiter():
    reason = acm._hunk_unsafe_reason("cat <<A\nBODY\n", "beta() {\n  echo b\n}\n", ".sh")
    assert reason is not None and "<<A" in reason, reason


def test_shell_side_with_closed_heredoc_is_still_additive():
    """Контроль на противоположную ошибку: сторона, которая открыла И
    закрыла heredoc, самостоятельна — отвергать её нельзя, иначе рубеж
    запретил бы любую вставку с текстовым телом."""
    ours = "alpha() {\n  cat <<'EOF'\ntext\nEOF\n}\n"
    theirs = "beta() {\n  echo b\n}\n"
    assert acm._hunk_unsafe_reason(ours, theirs, ".sh") is None


def test_shell_arithmetic_left_shift_is_not_a_heredoc():
    """`$(( x << SHIFT ))` — сдвиг влево, не heredoc. Спутать значит
    отвергать файл навсегда с ложной причиной (находка ai-ревью PR #1392)."""
    text = (
        "#!/usr/bin/env bash\n"
        "mask=$(( 1 << SHIFT ))\n"
        "(( y = z << BITS ))\n"
        "\n"
        "<<<<<<< HEAD\n"
        "alpha() {\n  echo a\n}\n"
        "=======\n"
        "beta() {\n  echo b\n}\n"
        ">>>>>>> branch\n"
    )
    resolved, reason = acm.resolve_file_text(text, ".sh")
    assert resolved is not None, reason
    assert "alpha()" in resolved and "beta()" in resolved


def test_shell_refuses_when_resolved_file_leaves_heredoc_open_at_eof(tmp_path):
    """Зеркальный рубеж на ИТОГОВОМ файле — случай, которого перехунковая
    проверка не видит по построению: heredoc открывает ХВОСТ файла, ниже
    хунка.

    Обе стороны самостоятельны, строки до хунка чисты — все перехунковые
    проверки зелёные. Но собранный файл остаётся с незакрытым heredoc'ом, и
    `bash -n` такое принимает (warning, rc=0, проверено исполнением). Файл в
    таком состоянии пришёл уже испорченным, и это ровно «любое сомнение —
    полный отказ» из контракта модуля: где именно кончаются данные, мы не
    знаем, а значит не знаем и того, не легла ли вставка внутрь них."""
    target = tmp_path / "tool.sh"
    target.write_text(
        "#!/usr/bin/env bash\n\n"
        "<<<<<<< HEAD\n"
        "alpha() {\n  echo a\n}\n"
        "=======\n"
        "beta() {\n  echo b\n}\n"
        ">>>>>>> branch\n"
        "cat <<TAIL\n"
        "данные без ограничителя\n",
        encoding="utf-8",
    )

    result = acm.try_resolve(tmp_path, ["tool.sh"])

    assert result is None, "итог с незакрытым heredoc сводить нельзя"
    assert "<<<<<<<" in target.read_text(encoding="utf-8"), "отказ ДО записи"


# ── закрытие heredoc по правилам настоящего bash ────────────────────────────
#
# Рубеж общий для всех трёх проверок heredoc (хунк внутри, сторона-открыватель,
# хвост итогового файла): правило `line.strip() == delim` закрывало heredoc
# строками, которые bash терминаторами НЕ считает. Оба негативных правила
# проверены прогоном НАСТОЯЩЕГО bash:
#   `cat <<'EOF'` … `EOF ` (хвостовой пробел) → тело продолжается, bash
#     предупреждает «here-document delimited by end-of-file»;
#   `cat <<-EOF` … `    EOF` (пробельный отступ) → то же: `<<-` снимает
#     только ТАБУЛЯЦИИ (таб-отступ закрывает — проверено тем же прогоном:
#     исполнение продолжается после терминатора).
# Оба случая воспроизведены end-to-end на голове до правки: try_resolve
# возвращал успех, и ОБЕ взаимоисключающие строки становились мёртвым телом
# heredoc'а итогового файла (bash -n зелёный).


def _conflict_after_false_terminator(open_line: str, bad_terminator: str) -> str:
    return (
        "#!/usr/bin/env bash\n"
        f"{open_line}\n"
        "prefix\n"
        f"{bad_terminator}\n"
        "<<<<<<< HEAD\n"
        "ours() {\n  echo ours\n}\n"
        "=======\n"
        "theirs() {\n  echo theirs\n}\n"
        ">>>>>>> branch\n"
        "EOF\n"
    )


def test_shell_heredoc_trailing_space_terminator_does_not_close(tmp_path):
    """`EOF ` с хвостовым пробелом — НЕ терминатор bash, конфликт после него
    всё ещё внутри ДАННЫХ и не сводится."""
    target = tmp_path / "tool.sh"
    target.write_text(
        _conflict_after_false_terminator("cat <<'EOF'", "EOF "), encoding="utf-8")

    assert acm.try_resolve(tmp_path, ["tool.sh"]) is None, (
        "heredoc не закрыт — конфликт внутри тела сводить нельзя")
    _, reason = acm.resolve_file_text(target.read_text(encoding="utf-8"), ".sh")
    assert "heredoc" in reason and "EOF" in reason, reason


def test_shell_dash_heredoc_space_indented_terminator_does_not_close(tmp_path):
    """`<<-` снимает с терминатора только табуляции: `    EOF` с пробелами
    её НЕ закрывает, конфликт после неё — данные."""
    target = tmp_path / "tool.sh"
    target.write_text(
        _conflict_after_false_terminator("cat <<-EOF", "    EOF"), encoding="utf-8")

    assert acm.try_resolve(tmp_path, ["tool.sh"]) is None


def test_shell_dash_heredoc_tab_indented_terminator_closes(tmp_path):
    """Контроль против перегиба: `<<-` с ТАБУЛЯЦИЕЙ — настоящий терминатор.
    Конфликт ПОСЛЕ него — обычный код и обязан свестись, иначе рубеж
    «залипал» бы и выключал поддержку шелла целиком."""
    target = tmp_path / "tool.sh"
    target.write_text(
        "#!/usr/bin/env bash\n"
        "cat <<-EOF\n"
        "\tbody\n"
        "\tEOF\n"
        "<<<<<<< HEAD\n"
        "alpha() {\n  echo a\n}\n"
        "=======\n"
        "beta() {\n  echo b\n}\n"
        ">>>>>>> branch\n",
        encoding="utf-8")

    assert acm.try_resolve(tmp_path, ["tool.sh"]) is not None
    merged = target.read_text(encoding="utf-8")
    assert "alpha()" in merged and "beta()" in merged, merged


def test_shell_heredoc_open_before_requires_exact_terminator():
    """Юнит на само правило: хвостовой пробел терминатора оставляет heredoc
    открытым; точное равенство закрывает."""
    opened = ["cat <<'EOF'", "prefix", "EOF ", "more"]
    assert acm._shell_heredoc_open_before(opened, 4).delim == "EOF"
    closed = ["cat <<'EOF'", "prefix", "EOF", "more"]
    assert acm._shell_heredoc_open_before(closed, 4) is None


def test_shell_heredoc_delimiter_may_start_with_digit():
    """`cat <<2` — легальный bash (прогон: тело печатается, строка «2»
    закрывает); пропуск открытия — отказ в опасную сторону. Широкий класс не
    открывает дверь маркерам: строка `<<<<<<< HEAD` не матчится и с ним."""
    assert acm._shell_heredoc_open_before(["cat <<2", "body"], 2).delim == "2"
    assert acm._shell_heredoc_open_before(
        ["<<<<<<< HEAD", "text", "=======", "text", ">>>>>>> branch"], 5) is None


# ── Раунд 4 ревью: форма открытия heredoc'а, а не только его закрытие ────
#
# Все bash-семантики ниже проверены прогоном НАСТОЯЩЕГО bash, не выведены
# из документации; дословные выводы — в теле PR #1392.


def _shell_conflict_inside(prologue: str, epilogue: str = "") -> str:
    """Файл, где конфликт стоит РОВНО там, куда целится сцена: сразу после
    `prologue`. Маркеры литеральные — это синтаксис самого git."""
    return (prologue
            + "<<<<<<< HEAD\nстрока владельца\n=======\n"
              "строка ветки\n>>>>>>> branch\n"
            + epilogue)


def test_shell_escaped_delimiter_opens_a_heredoc_and_is_not_missed():
    """`cat <<\\EOF` — легальный bash (прогон: тело печатается без подстановок,
    `EOF` закрывает). Прежний регексп не матчился вовсе, и конфликт в ТЕЛЕ
    сводился как код: все четыре рубежа шелла зеленели разом, потому что
    промах мимо открытия делает их слепыми одновременно."""
    text = _shell_conflict_inside("cat <<\\EOF\n", "EOF\n")

    resolved, reason = acm.resolve_file_text(text, ".sh")

    assert resolved is None, "конфликт в теле heredoc'а не имеет права сводиться"
    assert "heredoc" in reason and "EOF" in reason


def test_shell_heredoc_delimiter_is_read_as_a_whole_word():
    """`cat <<EOF-1`: настоящий bash держит данные до `EOF-1`, а строка `EOF`
    остаётся ТЕЛОМ (прогон). Прежний регексп брал ограничителем «EOF» — и
    хунк за этой строкой сводился как код, хотя он внутри данных."""
    assert acm._shell_heredoc_open_before(["cat <<EOF-1", "body"], 2).delim == "EOF-1"

    text = _shell_conflict_inside("cat <<EOF-1\nпролог тела\n", "EOF\nхвост\nEOF-1\n")
    resolved, reason = acm.resolve_file_text(text, ".sh")

    assert resolved is None
    assert "EOF-1" in reason


def test_shell_heredoc_delimiter_quotes_are_removed_like_bash_does():
    """Три написания одного ограничителя `EOF` — прогон настоящего bash
    подтвердил, что закрывает их все строка `EOF`, включая склейку
    `<<"EO"F`."""
    for opener in ("cat <<'EOF'", 'cat <<"EOF"', 'cat <<"EO"F', "cat <<E\\OF"):
        state = acm._shell_heredoc_open_before([opener, "body"], 2)
        assert state is not None and state.delim == "EOF", opener
        assert acm._shell_heredoc_open_before([opener, "body", "EOF"], 3) is None, opener


def test_shell_heredoc_word_stops_on_bash_metacharacters():
    """`cat <<EOF >out` и `cat <<EOF;echo TAIL` — ограничитель «EOF» (прогон:
    тело уходит в файл / хвост команды исполняется). Контроль против
    перегиба: рубеж не должен съедать хвост строки в ограничитель, иначе
    закрытый heredoc выглядел бы открытым и файл отказывался бы навсегда."""
    for opener in ("cat <<EOF >out.txt", "cat <<EOF;echo TAIL", "cat <<EOF | wc -l"):
        assert acm._shell_heredoc_open_before([opener, "body", "EOF"], 3) is None, opener


def test_shell_heredoc_hash_does_not_end_the_delimiter_word():
    """`cat <<EOF#x` — ограничитель «EOF#x» ЦЕЛИКОМ, строка `EOF` остаётся
    телом (прогон настоящего bash). `#` начинает комментарий только в начале
    слова, поэтому в список метасимволов он не входит."""
    state = acm._shell_heredoc_open_before(["cat <<EOF#x", "body", "EOF"], 3)

    assert state is not None and state.delim == "EOF#x"


def test_shell_unparsable_heredoc_form_refuses_loudly_and_says_why():
    """Контракт модуля — «любое сомнение — отказ», но сомнение обязано
    назвать СЕБЯ: «форму не разобрал» и «heredoc открыт» лечатся по-разному
    (AGENTS.md, «fail loud, не silent-wrong»)."""
    state = acm._shell_heredoc_open_before(["cat <<'EOF", "body"], 2)

    assert state is not None and state.delim is None
    assert "не опознана" in state.describe()

    resolved, reason = acm.resolve_file_text(
        _shell_conflict_inside("cat <<'EOF\nпролог\n"), ".sh")

    assert resolved is None
    assert "не опознана" in reason and "cat <<'EOF" in reason


def test_shell_closed_heredoc_still_lets_a_later_conflict_merge():
    """Контроль против перегиба всей пачки выше: правильно опознанный и
    ЗАКРЫТЫЙ heredoc не должен запирать файл — иначе новый разбор вылечил бы
    промах ценой отказа всем шелловым файлам с heredoc'ами."""
    text = _shell_conflict_inside("cat <<\\EOF\nтело\nEOF\n")
    text = text.replace("строка владельца", "alpha() { :; }")
    text = text.replace("строка ветки", "beta() { :; }")

    resolved, reason = acm.resolve_file_text(text, ".sh")

    assert resolved is not None, reason
    assert "alpha()" in resolved and "beta()" in resolved


# ── Сцены раунда доводки воркера, перенесённые на разбор слова ────────────
#
# Воркер транспорта закрывал те же две находки другим способом (широкий класс
# `[\w-]` + отдельный сканер «неопознанной формы»). Его СЦЕНЫ переносятся
# сюда целиком — они ценны и проверяют вход end-to-end через try_resolve; его
# МЕХАНИЗМ не переносится, и почему — в теле PR.


def test_shell_escaped_delimiter_refuses_end_to_end_through_try_resolve(tmp_path):
    """`cat <<\\EOF` разбирается в ограничитель «EOF» (прогон bash), конфликт
    стоит в ТЕЛЕ — отказ всей пачки, файл на диске не тронут."""
    target = tmp_path / "tool.sh"
    target.write_text(
        "#!/usr/bin/env bash\n"
        "cat <<\\EOF\n"
        "<<<<<<< HEAD\n"
        "ours() {\n  echo o\n}\n"
        "=======\n"
        "theirs() {\n  echo t\n}\n"
        ">>>>>>> branch\n"
        "EOF\n",
        encoding="utf-8")

    assert acm.try_resolve(tmp_path, ["tool.sh"]) is None
    _, reason = acm.resolve_file_text(target.read_text(encoding="utf-8"), ".sh")
    assert "heredoc (<<EOF)" in reason, reason


def test_shell_side_closing_its_own_escaped_heredoc_is_a_safe_insertion():
    """Контроль против перегиба: сторона, открывшая `<<\\A` и ЗАКРЫВШАЯ его
    своей же строкой `A`, самостоятельна — данные не утекают ниже, и вставка
    имеет право свестись. Пессимистичный отказ здесь выключил бы шелловые
    функции с heredoc'ами внутри."""
    resolved, reason = acm.resolve_file_text(
        "#!/usr/bin/env bash\n"
        "<<<<<<< HEAD\n"
        "emit() {\n  cat <<\\A\nbody\nA\n}\n"
        "=======\n"
        "beta() {\n  echo b\n}\n"
        ">>>>>>> branch\n",
        ".sh")

    assert resolved is not None, reason
    assert "emit()" in resolved and "beta()" in resolved


def test_shell_side_with_unparsable_heredoc_form_is_refused():
    """А форма, которую разобрать НЕЛЬЗЯ, делает сторону несамостоятельной:
    где кончаются её данные — неизвестно.

    Сцена выбрана так, чтобы краснел ИМЕННО этот рубеж, а не соседний
    (класс #891/#893 — зеленеть/краснеть не на том). `cat <<\\` с переносом
    строки — ЛЕГАЛЬНЫЙ bash: слэш склеивает строки, ограничителем становится
    `A` со следующей, `bash -n` возвращает 0 и тело реально печатается
    (прогон). То есть все прочие рубежи тут зелёные, и отказ может дать
    только неразобранная форма. Незакрытая кавычка на эту роль не годится:
    её первым ловит `bash -n`."""
    _, reason = acm.resolve_file_text(
        "#!/usr/bin/env bash\n"
        "<<<<<<< HEAD\n"
        "emit() {\n  cat <<\\\nA\nbody\nA\n}\n"
        "=======\n"
        "beta() {\n  echo b\n}\n"
        ">>>>>>> branch\n",
        ".sh")

    assert reason is not None and "не опознана" in reason, reason


def test_shell_hyphenated_delimiter_body_eof_does_not_close(tmp_path):
    """`cat <<EOF-1`: ограничитель — ПОЛНОЕ слово «EOF-1». Строка «EOF» в
    теле его НЕ закрывает (прогон bash), конфликт после неё — данные."""
    target = tmp_path / "tool.sh"
    target.write_text(
        "#!/usr/bin/env bash\n"
        "cat <<EOF-1\n"
        "EOF\n"
        "<<<<<<< HEAD\n"
        "ours() {\n  echo o\n}\n"
        "=======\n"
        "theirs() {\n  echo t\n}\n"
        ">>>>>>> branch\n"
        "EOF-1\n",
        encoding="utf-8")

    assert acm.try_resolve(tmp_path, ["tool.sh"]) is None
    _, reason = acm.resolve_file_text(target.read_text(encoding="utf-8"), ".sh")
    assert "heredoc (<<EOF-1)" in reason, reason


def test_shell_hyphenated_delimiter_real_terminator_merges(tmp_path):
    """Контроль против перегиба: настоящий терминатор «EOF-1» закрывает, и
    конфликт после него сводится как обычный код."""
    target = tmp_path / "tool.sh"
    target.write_text(
        "#!/usr/bin/env bash\n"
        "cat <<EOF-1\n"
        "body\n"
        "EOF-1\n"
        "<<<<<<< HEAD\n"
        "alpha() {\n  echo a\n}\n"
        "=======\n"
        "beta() {\n  echo b\n}\n"
        ">>>>>>> branch\n",
        encoding="utf-8")

    assert acm.try_resolve(tmp_path, ["tool.sh"]) is not None
    merged = target.read_text(encoding="utf-8")
    assert "alpha()" in merged and "beta()" in merged, merged


def test_shell_herestring_and_conflict_markers_do_not_open_anything():
    """`<<<` и маркеры конфликта — не открытие heredoc'а и не «неопознанная
    форма»: обе границы регекспа отбрасывают все позиции внутри них.
    Арифметика вырезается до поиска. Ложный сигнал здесь выключил бы шелл
    целиком."""
    assert acm._shell_heredoc_open_before(
        ["grep x <<< HELLO", "<<<<<<< HEAD"], 2) is None
    assert acm._shell_heredoc_open_before(["x=$((a << b))"], 1) is None
    assert acm._shell_heredoc_open_before(
        ["<<<<<<< HEAD", "t", "=======", "t", ">>>>>>> branch"], 5) is None


# ── раунд 5: полные строки ключевых слов; пин сцены склейки ограничителя ────
#
# Multi-assign — ложное «безопасно»: неполный список имён пропускал
# столкновение второго имени в `export A=1 B=2`, конкатенация молча оставляла
# живым последнее присваивание (класс #883). Пин сцены `<<'E'O` — по одному
# месту: разбор слова bash закрывает её (раунд 4), сцена должна ОСТАВАТЬСЯ
# отказом при любом будущем рефакторинге сканера.


def test_shell_multi_name_keyword_line_collision_is_caught():
    """`export A=1 B=2` определяет ОБА имени: форма, ловившая только первое,
    давала ложное «имена не пересеклись» там, где обе стороны правят второй
    (класс #883 — конкатенация молча оставляет живым последнее)."""
    for ours, theirs, expected in (
        ("export A=1 B=2\n", "export B=9 C=3\n", "B"),
        ("readonly E1=x E2=y\n", "E2=z\n", "E2"),
        ("alias ll='ls -l' lsl='ls -la'\n", "lsl=/bin/lsl\n", "lsl"),
    ):
        reason = acm._hunk_unsafe_reason(ours, theirs, ".sh")
        assert reason is not None and expected in reason, (ours, theirs, reason)


def test_shell_multi_name_keyword_line_without_collision_is_additive():
    ours = "export A=1 B=2\n"
    theirs = "export C=3 D=4\n"
    assert acm._hunk_unsafe_reason(ours, theirs, ".sh") is None


def test_shell_duplicate_count_does_not_double_count_keyword_lines():
    """Строка `export A=1 B=2` обязана посчитать `A` РОВНО один раз: две
    формы разбора на одной строке дали бы «дубликат из одной строки» и
    отвергали бы годовый файл."""
    assert acm._duplicate_shell_top_level_names("export A=1 B=2\n") == []
    assert acm._duplicate_shell_top_level_names("readonly E1=x E1=y\n") == ["E1"]


def test_shell_quoted_value_with_equals_is_not_a_name():
    """`--color=auto` внутри значения alias — ДАННЫЕ, не определение имени:
    фантом создал бы ложные пересечения (отказ без нужды). Флаги и кавычки
    перед `=` исключают имя; `color=red` второй стороны остаётся честным
    верхнеуровневым именем и ни с чем не сталкивается."""
    ours = "alias grep='grep --color=auto'\n"
    theirs = "color=red\n"
    assert acm._hunk_unsafe_reason(ours, theirs, ".sh") is None


def test_shell_quoted_delim_concatenation_scene_stays_refused(tmp_path):
    """Пин исполненной сцены пробы (раунд 5 PR #1392): ours открывает
    `<<'E'O` — ограничитель EO (кавычка и слово склеиваются, прогон
    настоящего bash), в теле есть строка «E». Регексп раундов 1-3 брал
    ограничителем усечённое «E» и закрывал на ней heredoc: merge проходил,
    а при ИСПОЛНЕНИИ сведённого файла `beta()` не определялась вовсе
    (bash -n rc=0). Разбор слова bash раунда 4 закрывает сцену — она обязана
    оставаться отказом, отказ обязан называть ПОЛНОЕ имя, не усечённое."""
    target = tmp_path / "tool.sh"
    target.write_text(
        "#!/usr/bin/env bash\n\n"
        "<<<<<<< HEAD\n"
        "cat <<'E'O\n"
        "body\n"
        "E\n"
        "tail-code\n"
        "=======\n"
        "beta() {\n  echo b\n}\n"
        ">>>>>>> branch\n",
        encoding="utf-8",
    )

    result = acm.try_resolve(tmp_path, ["tool.sh"])

    assert result is None, "склейка ограничителя — не самостоятельная вставка"
    _, reason = acm.resolve_file_text(target.read_text(encoding="utf-8"), ".sh")
    assert reason is not None and "EO" in reason and "<<E'" not in reason, reason


def test_shell_phantom_opening_refuses_never_merges_silently():
    """Честная граница сканера, запиненная НАПРАВЛЕНИЕМ, а не списком сцен
    (некритичная находка ai-ревью PR #1392, раунд 5).

    Кавыченный текст и комментарий сканер от кода не отличает, поэтому
    открывает фантомы там, где bash heredoc'а не видит (все четыре строки
    ниже настоящий bash исполняет без единой жалобы, rc=0). Цена — лишний
    уход файла в агентский путь. Чего НЕ бывает: тихого сведения. Очередь
    `pending` от лишнего открытия только растёт и снимается строго с головы,
    поэтому фантом не может закрыть настоящий heredoc раньше срока — тест
    проверяет именно это, а не конкретные формулировки отказа."""
    phantoms = [
        'echo "usage: tool <<option>> value"',
        "echo 'sed s/<<>/X/'",
        'let "mask = 1 << 4"',
        "# пример: cat <<EOF",
    ]
    for line in phantoms:
        assert acm._shell_heredoc_open_before([line, "код"], 2) is not None, line

    # Фантом ПОСЛЕ настоящего открытия в той же строке: настоящий закрывается
    # своим терминатором, фантом остаётся — файл отказан, а не сведён.
    tail_open = acm._shell_heredoc_open_before(
        ["cat <<EOF # пример: cat <<PHANTOM", "тело", "EOF", "код"], 4)
    assert tail_open is not None and tail_open.delim == "PHANTOM"

    # Контроль: чистая строка кода фантома не порождает — пессимизм не
    # разлился на весь шелл.
    assert acm._shell_heredoc_open_before(["alpha() { echo a; }", "код"], 2) is None
    assert acm._shell_heredoc_open_before(["x=$((a << b))", "код"], 2) is None


def test_shell_heredoc_queue_closes_strictly_from_the_head():
    """`cmd <<A <<B` — очередь снимается СТРОГО с головы, и это правило
    настоящего bash, а не удобство реализации.

    Прогон (скрипт `cat <<A <<B` / `тело A` / `B` / `ещё тело A` / `A` /
    `тело B` / `B` / `echo AFTER`): bash печатает «тело B» и «AFTER», rc=0 —
    то есть строка `B` ВНУТРИ тела A осталась ДАННЫМИ, тело A кончилось на
    `A`, и только следующая `B` закрыла второй heredoc.

    Снятие не с головы (закрыть любой совпавший ограничитель) объявило бы
    файл чистым на строке «тело B» — а она ещё ДАННЫЕ. Это тихое сведение,
    то есть опасная сторона, поэтому правило и закреплено тестом."""
    lines = ["cat <<A <<B", "тело A", "B", "ещё тело A", "A", "тело B", "B", "код"]

    # Строка 3 (`B`) — данные тела A: heredoc всё ещё A, а не закрыт.
    assert acm._shell_heredoc_open_before(lines, 4).delim == "A"
    # После настоящего терминатора A открытым остаётся B.
    assert acm._shell_heredoc_open_before(lines, 6).delim == "B"
    # И только вторая `B` закрывает очередь целиком.
    assert acm._shell_heredoc_open_before(lines, 8) is None
