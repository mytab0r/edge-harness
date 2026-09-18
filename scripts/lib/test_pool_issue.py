#!/usr/bin/env python3
"""Тесты create_pool_issue (#526, #720) — единственная точка программного заведения
issue пула, которая физически не даёт создать issue без `task` и без
машиночитаемого объявления связи.

Мутация, которой доказана проверка метки task: закомментируй `if REQUIRED_LABEL not in
labels: raise ...` в scripts/lib/pool_issue.py — test_missing_task_label_*
перестают падать при отсутствии task и начинают звать fake gh, тест
краснеет (см. test_missing_task_label_never_calls_gh — считает вызовы gh).

Мутация, которой доказана проверка объявления связи (#720): закомментируй
`if not _has_declared_dependency(body): raise ...` — тесты
test_missing_declared_dependency_* перестают падать и начинают звать fake gh,
тест краснеет.

Мутация, которой доказан перенос объявления в _append_note (#720, находка
ревью PR #804): замени тело _append_note на `return body + note` —
test_append_note_moves_inline_declaration_after_note и
test_append_note_keeps_inline_declaration_with_numbers краснеют (объявление
перестаёт быть последней непустой строкой).

Распознавание здесь НЕ своя копия регэкспа: `_has_declared_dependency`
делегирует существующим объединённым читателям declared_deps —
`declared_blocked_by` (структурное поле «Чем блокируется» + инлайн-строка)
и `blocking_field_numbers` (обратное поле «Что блокирует», задача #710) —
тот же предикат, что читают `auto_wire`/`repo_invariants`; тесты ниже кормят
гейт прод-формами всех трёх записей, включая пограничные (ответ из одних
пробелов — случай, на котором awk-копия первого захода PR #804 разошлась с
Python-гейтом; неразобранный ответ обратного поля — данные есть, качество
ответа не дело гейта).

CLI `check-body` (единственный парсер для bash-обёртки scripts/gh/issue-create)
проверяется живым подпроцессом: rc/поток/готовая строка в stderr.

Запуск: python -m pytest scripts/lib/test_pool_issue.py -q
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parent
SCRIPT = _DIR / "pool_issue.py"
spec = importlib.util.spec_from_file_location("pool_issue", SCRIPT)
pool_issue = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pool_issue)  # type: ignore[union-attr]


class RecordingGh:
    """Фейковый gh(*args): считает вызовы и отдаёт прод-форму ответа
    POST .../issues (число + html_url), без реальной сети."""

    def __init__(self):
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str):
        self.calls.append(args)
        return {"number": 999, "html_url": "https://github.com/o/r/issues/999"}


def test_missing_task_label_never_calls_gh():
    gh = RecordingGh()
    with pytest.raises(RuntimeError, match="task"):
        pool_issue.create_pool_issue(gh, "o/r", "title", "body", ["white-spot"])
    assert gh.calls == []


def test_empty_labels_never_calls_gh():
    gh = RecordingGh()
    with pytest.raises(RuntimeError, match="task"):
        pool_issue.create_pool_issue(gh, "o/r", "title", "body", [])
    assert gh.calls == []


def test_task_label_present_calls_gh_with_post_issues():
    gh = RecordingGh()
    # Тело с валидным объявлением связи («ничем» через структурное поле)
    body = "### Чем блокируется\nничем\n"
    created = pool_issue.create_pool_issue(gh, "o/r", "title", body, ["task"])
    assert created["number"] == 999
    assert len(gh.calls) == 1
    args = gh.calls[0]
    assert args[:3] == ("-X", "POST", "repos/o/r/issues")
    assert "-f" in args and "title=title" in args
    # body передается как один аргумент '-f', 'body=...' — проверяем, что
    # среди аргументов есть строка, начинающаяся с 'body='
    assert any(a.startswith("body=") for a in args)
    assert "labels[]=task" in args


def test_multiple_labels_all_forwarded():
    gh = RecordingGh()
    body = "### Чем блокируется\nничем\n"
    pool_issue.create_pool_issue(gh, "o/r", "t", body, ["task", "white-spot"])
    args = gh.calls[0]
    assert "labels[]=task" in args
    assert "labels[]=white-spot" in args


# ── #720: проверка машиночитаемого объявления связи ────────────────────────

def test_missing_declared_dependency_never_calls_gh():
    """Тело без объявления связи — RuntimeError, gh не вызван."""
    gh = RecordingGh()
    with pytest.raises(RuntimeError, match="машиночитаемого объявления связи"):
        pool_issue.create_pool_issue(gh, "o/r", "title", "просто тело без связи", ["task"])
    assert gh.calls == []


def test_declared_dependency_nichem_structural_field_calls_gh():
    """Структурное поле с ответом 'ничем' — валидно, gh вызван."""
    gh = RecordingGh()
    body = "### Чем блокируется\nничем\n"
    created = pool_issue.create_pool_issue(gh, "o/r", "title", body, ["task"])
    assert created["number"] == 999
    assert len(gh.calls) == 1


def test_declared_dependency_nichem_inline_calls_gh():
    """Инлайн-строка 'БЛОКИРУЕТСЯ: ничем' — валидно, gh вызван."""
    gh = RecordingGh()
    body = "Какое-то тело\n\nБЛОКИРУЕТСЯ: ничем"
    created = pool_issue.create_pool_issue(gh, "o/r", "title", body, ["task"])
    assert created["number"] == 999
    assert len(gh.calls) == 1


def test_declared_dependency_with_numbers_structural_calls_gh():
    """Структурное поле с номерами — валидно, gh вызван."""
    gh = RecordingGh()
    body = "### Чем блокируется\n#123 #456\n"
    created = pool_issue.create_pool_issue(gh, "o/r", "title", body, ["task"])
    assert created["number"] == 999
    assert len(gh.calls) == 1


def test_declared_dependency_with_numbers_inline_calls_gh():
    """Инлайн-строка с номерами — валидно, gh вызван."""
    gh = RecordingGh()
    body = "Какое-то тело\n\nБЛОКИРУЕТСЯ: #123 #456"
    created = pool_issue.create_pool_issue(gh, "o/r", "title", body, ["task"])
    assert created["number"] == 999
    assert len(gh.calls) == 1


def test_declared_dependency_various_header_levels():
    """Разные уровни заголовка (##, ###, ####) — все валидны."""
    for header in ["## Чем блокируется", "### Чем блокируется", "#### Чем блокируется"]:
        gh = RecordingGh()
        body = f"{header}\nничем\n"
        created = pool_issue.create_pool_issue(gh, "o/r", "title", body, ["task"])
        assert created["number"] == 999
        assert len(gh.calls) == 1


def test_declared_dependency_empty_answer_valid():
    """Пустой ответ после заголовка — валидно (как 'ничем')."""
    gh = RecordingGh()
    body = "### Чем блокируется\n\n"
    created = pool_issue.create_pool_issue(gh, "o/r", "title", body, ["task"])
    assert created["number"] == 999
    assert len(gh.calls) == 1


def test_declared_dependency_whitespace_only_answer_line_valid():
    """Ответ из одних пробелов между заголовком и «ничем» — валиден.

    Живой случай — находка AI-ревью PR #804: awk-копия проверки считала
    строку из пробелов «ответом-прозой» и отказывала тело, которое
    Python-гейт и declared_deps (место правды) принимали. Теперь парсер
    один — гейт обязан соглашаться с читателем графа на этой же форме."""
    gh = RecordingGh()
    body = "### Чем блокируется\n   \nничем\n"
    created = pool_issue.create_pool_issue(gh, "o/r", "title", body, ["task"])
    assert created["number"] == 999
    assert len(gh.calls) == 1


def test_declared_dependency_bare_numbers_in_field_valid():
    """Голый номер без `#` в ответе поля — валиден (то же, что читает
    auto_wire: declared_deps._VALUE_NUMBER_RE берёт `#?\\d{2,5}`; гейт,
    требующий `#`, отвергал бы тело, которое граф потом разберёт)."""
    gh = RecordingGh()
    body = "### Чем блокируется\n55\n"
    created = pool_issue.create_pool_issue(gh, "o/r", "title", body, ["task"])
    assert created["number"] == 999
    assert len(gh.calls) == 1


def test_empty_body_never_calls_gh():
    """Пустое тело — объявления нет, отказ ДО сети (то же, что ответит
    check-body bash-обёртке; первый заход PR #804 пустое тело пропускал)."""
    gh = RecordingGh()
    with pytest.raises(RuntimeError, match="машиночитаемого объявления связи"):
        pool_issue.create_pool_issue(gh, "o/r", "title", "", ["task"])
    assert gh.calls == []


# ── #720: обратное поле «Что блокирует» — та же связь, другое направление ────
# Задача #710: поле «Что блокирует: B» переносится auto_wire'ом с разворотом
# (B получает blockedBy A), то есть тело с этим полем УЖЕ несёт разбираемую
# связь. Гейт, требующий только прямого поля/инлайна, отказывал бы телу,
# которое граф потом разберёт (находка ревью PR #804).

def test_declared_dependency_reverse_field_calls_gh():
    """Обратное поле «Что блокирует» с номерами — валидно, gh вызван."""
    gh = RecordingGh()
    body = "### Что блокирует\n#123\n"
    created = pool_issue.create_pool_issue(gh, "o/r", "title", body, ["task"])
    assert created["number"] == 999
    assert len(gh.calls) == 1


def test_declared_dependency_reverse_field_nichem_calls_gh():
    """Обратное поле с ответом «ничем» — валидно, gh вызван."""
    gh = RecordingGh()
    body = "## Что блокирует\nничем\n"
    created = pool_issue.create_pool_issue(gh, "o/r", "title", body, ["task"])
    assert created["number"] == 999
    assert len(gh.calls) == 1


def test_declared_dependency_reverse_field_unparseable_answer_calls_gh():
    """Обратное поле с неразобранным ответом — данные на входе есть, гейт
    пропускает (UNRECOGNIZED_FORM — не None; качество ответа — дело
    auto_wire, а не гейта заведения; то же правило, что у прямого поля)."""
    gh = RecordingGh()
    body = "### Что блокирует\nвсё упирается в согласование\n"
    created = pool_issue.create_pool_issue(gh, "o/r", "title", body, ["task"])
    assert created["number"] == 999
    assert len(gh.calls) == 1


# ── #720: _append_note — приписка не разрушает объявление ────────────────────
# Живой случай — находка ревью PR #804: футер --confirm-not-duplicate
# дописывался к проверенному телу, и инлайн-объявление переставало быть
# последней непустой строкой — созданная issue объявление не несла.

def test_append_note_moves_inline_declaration_after_note():
    """Инлайн-объявление переносится в конец ЗА приписку — объявление
    остаётся последней непустой строкой созданного тела."""
    body = "Тело задачи\nБЛОКИРУЕТСЯ: ничем"
    result = pool_issue._append_note(body, "\n\n---\nПриписка про дубли")
    lines = [line.strip() for line in result.splitlines() if line.strip()]
    assert lines[-1] == "БЛОКИРУЕТСЯ: ничем"
    assert "Приписка про дубли" in result
    # итог проходит тот же предикат
    assert pool_issue._has_declared_dependency(result)


def test_append_note_keeps_inline_declaration_with_numbers():
    """Инлайн с номерами переносится дословно — формат не перечитывается."""
    body = "Тело\nБЛОКИРУЕТСЯ: #12 #34"
    result = pool_issue._append_note(body, "\n---\nПриписка")
    lines = [line.strip() for line in result.splitlines() if line.strip()]
    assert lines[-1] == "БЛОКИРУЕТСЯ: #12 #34"


def test_append_note_structural_body_untouched():
    """Тело со структурным полем приписка не переставляет: заголовок с
    ответом положения последней строки не обязан — после приписки
    предикат и так true."""
    body = "### Чем блокируется\n#55\n"
    result = pool_issue._append_note(body, "\n---\nПриписка")
    assert result == body + "\n---\nПриписка"
    assert pool_issue._has_declared_dependency(result)


def test_append_note_without_declaration_unchanged():
    """Тело без объявления не «чинится» — его не пропустил бы гейт
    заведения; _append_note не второй гейт и молча дописывать «ничем»
    за автора не должен."""
    body = "просто тело без связи"
    result = pool_issue._append_note(body, "\n---\nПриписка")
    assert result == body + "\n---\nПриписка"
    assert not pool_issue._has_declared_dependency(result)


def test_append_note_cli_writes_body_with_declaration_last(tmp_path):
    """CLI append-note: тело из stdin, приписка из файла, объявление —
    последней непустой строкой результата."""
    note_file = tmp_path / "note.txt"
    note_file.write_text("\n---\nПриписка", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "append-note", str(note_file)],
        input="Тело задачи\nБЛОКИРУЕТСЯ: ничем".encode("utf-8"),
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8")
    out = proc.stdout.decode("utf-8")
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    assert lines[-1] == "БЛОКИРУЕТСЯ: ничем"
    assert "Приписка" in out


# ── #720: CLI check-body — единственный парсер для bash-обёртки ─────────────

def _run_check_body(body: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "check-body"],
        input=body.encode("utf-8"),
        capture_output=True,
        timeout=60,
    )


def test_check_body_cli_accepts_both_forms():
    rc_structural = _run_check_body("### Чем блокируется\nничем\n")
    assert rc_structural.returncode == 0, rc_structural.stderr.decode("utf-8")
    rc_inline = _run_check_body("Тело задачи\nБЛОКИРУЕТСЯ: #720\n")
    assert rc_inline.returncode == 0, rc_inline.stderr.decode("utf-8")


def test_check_body_cli_refuses_without_declaration_and_prints_hint():
    rc = _run_check_body("просто тело без связи")
    assert rc.returncode == 1
    err = rc.stderr.decode("utf-8")
    assert "машиночитаемого объявления связи" in err
    # газ: готовая строка для вставки
    assert "### Чем блокируется" in err
    assert "ничем" in err


def test_check_body_cli_refuses_empty_body():
    rc = _run_check_body("")
    assert rc.returncode == 1
    assert "### Чем блокируется" in rc.stderr.decode("utf-8")
