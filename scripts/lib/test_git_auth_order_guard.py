#!/usr/bin/env python3
"""Стенд гвардии порядка авторизации git (#1486).

Стенд пишет НАСТОЯЩИЕ файлы workflow во временный каталог и гоняет по ним
настоящую функцию разбора; скрипты в шагах — настоящие файлы репозитория, а не
выдуманные имена, иначе проверялся бы разбор строки, а не свойство.

Запуск: python -m pytest scripts/lib/test_git_auth_order_guard.py -q
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import sys
import textwrap

import pytest

_spec = importlib.util.spec_from_file_location(
    "git_auth_order_guard", Path(__file__).resolve().parent / "git_auth_order_guard.py")
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

#: Настоящий писатель репозитория: `scheduler.py` грузит `pulse_guard.py`,
#: тот — `telegram_topics.py`, и только он пишет на data-ветку. Цепочка
#: транзитивная, ровно тот случай, ради которого гвардия и написана.
WRITER_SCRIPT = "scripts/orchestra/scheduler.py"


def _write(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


# ── Транзитивность ──────────────────────────────────────────────────────────

def test_writer_is_found_through_the_real_dependency_chain():
    """Прямой разбор импортов эту цепочку не видит: соседние библиотеки здесь
    подключаются через spec_from_file_location, а не обычным import."""
    assert guard.writes_data_branch(guard.REPO_ROOT / WRITER_SCRIPT)


def test_the_writer_module_itself_is_a_writer():
    assert guard.writes_data_branch(guard.REPO_ROOT / guard.WRITER_MODULE)


def test_a_script_without_the_chain_is_not_a_writer():
    """Ложноположительное срабатывание дороже пропуска: оно заставит добавить
    авторизацию туда, где креды другие."""
    assert not guard.writes_data_branch(
        guard.REPO_ROOT / "scripts/lib/console_utf8.py")


# ── Порядок шагов ───────────────────────────────────────────────────────────

def test_auth_after_the_writer_is_a_loud_refusal(tmp_path):
    """Ровно инцидент #1486: шаг авторизации есть, но ниже пишущего."""
    _write(tmp_path, "wf.yml", f"""
        name: wf
        on: {{workflow_dispatch: {{}}}}
        jobs:
          j:
            runs-on: ubuntu-latest
            steps:
              - name: Пишет
                run: python {WRITER_SCRIPT}
              - name: Авторизация
                run: gh auth setup-git
        """)
    found = guard.problems(tmp_path)
    assert len(found) == 1, found
    assert "раньше, чем `gh auth setup-git`" in found[0]
    assert "wf.yml:j" in found[0]


def test_auth_before_the_writer_passes(tmp_path):
    """Положительная сторона того же пути — иначе гвардия краснела бы всегда."""
    _write(tmp_path, "wf.yml", f"""
        name: wf
        on: {{workflow_dispatch: {{}}}}
        jobs:
          j:
            runs-on: ubuntu-latest
            steps:
              - name: Авторизация
                run: gh auth setup-git
              - name: Пишет
                run: python {WRITER_SCRIPT}
        """)
    assert guard.problems(tmp_path) == []


def test_missing_auth_is_a_refusal_naming_the_gas(tmp_path):
    """Новый job с писателем и без авторизации не появится молча: отказ
    называет оба выхода — добавить шаг либо вписать причину."""
    _write(tmp_path, "brand-new.yml", f"""
        name: brand-new
        on: {{workflow_dispatch: {{}}}}
        jobs:
          j:
            runs-on: ubuntu-latest
            steps:
              - name: Пишет
                run: python {WRITER_SCRIPT}
        """)
    found = guard.problems(tmp_path)
    assert len(found) == 1, found
    assert "ALLOWED_WITHOUT_GIT_AUTH" in found[0]


def test_recorded_debt_is_allowed_but_only_by_name(tmp_path, monkeypatch):
    """Долг записан — не краснеет. Записан ДРУГОЙ job — краснеет: газ адресный,
    а не общий выключатель."""
    body = f"""
        name: wf
        on: {{workflow_dispatch: {{}}}}
        jobs:
          j:
            runs-on: ubuntu-latest
            steps:
              - name: Пишет
                run: python {WRITER_SCRIPT}
        """
    _write(tmp_path, "wf.yml", body)
    monkeypatch.setitem(guard.ALLOWED_WITHOUT_GIT_AUTH, "wf.yml:j", "причина")
    assert guard.problems(tmp_path) == []

    monkeypatch.delitem(guard.ALLOWED_WITHOUT_GIT_AUTH, "wf.yml:j")
    monkeypatch.setitem(guard.ALLOWED_WITHOUT_GIT_AUTH, "wf.yml:другой", "причина")
    assert len(guard.problems(tmp_path)) == 1


def test_auth_is_recognised_by_the_command_not_by_the_step_name(tmp_path):
    """Имя шага — проза и меняется; `gh auth setup-git` — факт."""
    _write(tmp_path, "wf.yml", f"""
        name: wf
        on: {{workflow_dispatch: {{}}}}
        jobs:
          j:
            runs-on: ubuntu-latest
            steps:
              - name: Что-то совсем другое
                run: |
                  echo подготовка
                  gh auth setup-git
              - name: Пишет
                run: python {WRITER_SCRIPT}
        """)
    assert guard.problems(tmp_path) == []


def test_each_job_is_judged_on_its_own(tmp_path):
    """Авторизация в соседнем job'е не помогает: у каждого job'а свой runner и
    свой клон."""
    _write(tmp_path, "wf.yml", f"""
        name: wf
        on: {{workflow_dispatch: {{}}}}
        jobs:
          first:
            runs-on: ubuntu-latest
            steps:
              - name: Авторизация
                run: gh auth setup-git
          second:
            runs-on: ubuntu-latest
            steps:
              - name: Пишет
                run: python {WRITER_SCRIPT}
        """)
    found = guard.problems(tmp_path)
    assert len(found) == 1 and "wf.yml:second" in found[0], found


# ── Замок на живом репозитории ──────────────────────────────────────────────

def test_the_repository_itself_is_clean():
    """Перестановка `gh auth setup-git` обратно вниз в orchestra.yml обязана
    покраснеть — ради этого гвардия и написана."""
    assert guard.problems() == []


def test_every_recorded_debt_carries_a_reason():
    """Запись без причины — это тихий выключатель, а не объявленный газ."""
    empty = [key for key, reason in guard.ALLOWED_WITHOUT_GIT_AUTH.items()
             if not str(reason).strip()]
    assert empty == [], empty


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
