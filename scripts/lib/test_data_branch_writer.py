#!/usr/bin/env python3
"""Тесты `scripts/lib/data_branch_writer.py` (issue #882).

Живой инцидент: `push` в ветку данных падал 403 (клон в `$RUNNER_TEMP` не
наследует креды `actions/checkout`, `gh auth setup-git` не вызывался), но
восстановление (`fetch`/`checkout`) шло с `check=False` — отказ проглатывался,
и следующая итерация цикла перечитывала СВОЙ же незапушенный локальный
коммит, принимая его за «уже записано параллельным прогоном». Три класса
доказательств здесь:

  1. `classify_push_failure` — чистая функция на синтетическом stderr, без
     сети (гонка vs авторизация различимы по тексту).
  2. Гонка — настоящий git, два клона одного bare-репозитория (тот же приём,
     что `scripts/measure/test_dispatch_tail.py::git_writer_fixture`):
     конкурент пушит первым, второй писатель обязан пережить это и записать
     свою строку поверх.
  3. Отказ авторизации — настоящий git, `pre-receive`-хук bare-репозитория
     отклоняет ИМЕННО ветку данных сообщением, дословно похожим на реальный
     GitHub 403 ("Permission to ... denied", "failed to push some refs") —
     `append_and_push` обязан упасть ГРОМКО (RuntimeError), не тихо решить
     «кто-то другой уже записал».
  4. Первое создание ветки: bare-репозиторий без единого коммита на ветке
     данных вовсе — `clone_data_branch` обязан завести её от `main`, а не
     упасть на отсутствующем `origin/<br>`.

Мутация (доказательство, что тест реально ловит регресс, не просто зелёный):
верни в `append_and_push` старое `check=False` на восстановлении после отказа
push — `test_append_and_push_raises_loud_on_auth_rejection` и
`test_append_and_push_race_survivor_confirms_from_fresh_fetch` красные (см.
процедуру мутации в описании PR).

Запуск: python -m pytest scripts/lib/test_data_branch_writer.py -q
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parent
SCRIPT = _DIR / "data_branch_writer.py"
spec = importlib.util.spec_from_file_location("data_branch_writer", SCRIPT)
dbw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dbw)  # type: ignore[union-attr]


# ── classify_push_failure: чистая функция, синтетические строки ─────────────


@pytest.mark.parametrize("stderr", [
    "! [rejected]        HEAD -> data/pipeline-health (non-fast-forward)\n"
    "error: failed to push some refs to 'https://github.com/o/r.git'\n"
    "hint: Updates were rejected because the tip of your current branch is behind\n",
    "To https://github.com/o/r.git\n"
    " ! [rejected]  HEAD -> data/x (fetch first)\n"
    "error: failed to push some refs to 'https://github.com/o/r.git'\n",
])
def test_classify_push_failure_race(stderr):
    assert dbw.classify_push_failure(stderr) == "race"


@pytest.mark.parametrize("stderr", [
    "remote: Permission to owner/repo.git denied to github-actions[bot].\n"
    "fatal: unable to access 'https://github.com/owner/repo.git/': The requested "
    "URL returned error: 403\n",
    "fatal: could not read Username for 'https://github.com': terminal prompts disabled\n",
    "remote: Invalid username or password.\n"
    "fatal: Authentication failed for 'https://github.com/owner/repo.git/'\n",
])
def test_classify_push_failure_auth(stderr):
    assert dbw.classify_push_failure(stderr) == "auth"


def test_classify_push_failure_auth_wins_over_generic_race_tail():
    """Реальный 403 несёт ОБА класса маркеров разом (generic "failed to push
    some refs" — хвост любого отклонённого push, не только гонки): маркеры
    прав обязаны проверяться первыми, иначе это классифицировалось бы как
    безобидная гонка (см. докстринг модуля)."""
    stderr = (
        "remote: Permission to owner/repo.git denied to actor.\n"
        "remote: error: GH006: Protected branch update failed\n"
        "error: failed to push some refs to 'https://github.com/owner/repo.git'\n"
    )
    assert dbw.classify_push_failure(stderr) == "auth"


def test_classify_push_failure_unknown_is_not_race():
    assert dbw.classify_push_failure("fatal: some completely unrelated git error\n") == "unknown"
    assert dbw.classify_push_failure("") == "unknown"


# ── Git-транспорт: настоящий git, локальный bare-репозиторий ────────────────


def git_writer_fixture(tmp_path, name):
    bare = tmp_path / "bare.git"
    if not bare.exists():
        subprocess.run(["git", "init", "--quiet", "--bare", "--initial-branch=main", str(bare)],
                       check=True)
        seed = tmp_path / "seed"
        subprocess.run(["git", "clone", "--quiet", str(bare), str(seed)], check=True)
        subprocess.run(["git", "-C", str(seed), "checkout", "--quiet", "-B", "main"], check=True)
        (seed / "README.md").write_text("x", encoding="utf-8")
        subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
        subprocess.run(["git", "-C", str(seed), "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "--quiet", "-m", "seed"], check=True)
        subprocess.run(["git", "-C", str(seed), "push", "--quiet", "origin", "main"], check=True)
    work = tmp_path / name
    subprocess.run(["git", "clone", "--quiet", str(bare), str(work)], check=True)
    subprocess.run(["git", "-C", str(work), "checkout", "--quiet", "-B", "data/writer-test",
                    "origin/main"], check=True)
    return bare, work


COMMIT_IDENTITY = ("-c", "user.name=t", "-c", "user.email=t@t")


def _line_writer(text: str):
    def is_duplicate(current: str) -> bool:
        return text in current.splitlines()

    def render_next(current: str) -> str:
        return current + text + "\n"
    return is_duplicate, render_next


def test_append_and_push_writes_and_creates_branch_first_time(tmp_path):
    """Требование «путь первого создания обязан работать» (#882): ветка
    data/writer-test не существует на origin до этого вызова вовсе."""
    _, work = git_writer_fixture(tmp_path, "a")
    is_dup, render = _line_writer("row-1")
    assert dbw.append_and_push(str(work), "data/writer-test", "data.txt",
                               COMMIT_IDENTITY, "row 1", is_dup, render) is True
    assert (work / "data.txt").read_text(encoding="utf-8") == "row-1\n"


def test_append_and_push_local_duplicate_short_circuits(tmp_path):
    _, work = git_writer_fixture(tmp_path, "a")
    is_dup, render = _line_writer("row-1")
    assert dbw.append_and_push(str(work), "data/writer-test", "data.txt",
                               COMMIT_IDENTITY, "row 1", is_dup, render) is True
    assert dbw.append_and_push(str(work), "data/writer-test", "data.txt",
                               COMMIT_IDENTITY, "row 1 again", is_dup, render) is False


def test_append_and_push_race_survivor_confirms_from_fresh_fetch(tmp_path):
    """Гонка: конкурент пушит первым. Писатель обязан ГРОМКО перечитать
    ветку (fetch+checkout, не check=False) и записать свою строку поверх —
    «чужая запись существует» проверяется фактом с сервера, не локальным
    кэшем (#882, требование 1)."""
    bare, writer_a = git_writer_fixture(tmp_path, "a")
    _, writer_b = git_writer_fixture(tmp_path, "b")

    is_dup_x, render_x = _line_writer("row-x")
    assert dbw.append_and_push(str(writer_a), "data/writer-test", "data.txt",
                               COMMIT_IDENTITY, "row x", is_dup_x, render_x) is True

    # writer_b не видел коммит writer_a — пушит первым и выигрывает.
    is_dup_y, render_y = _line_writer("row-y")
    assert dbw.append_and_push(str(writer_b), "data/writer-test", "data.txt",
                               COMMIT_IDENTITY, "row y", is_dup_y, render_y) is True

    # writer_a продолжает поверх подвинувшейся ветки — его новая строка не теряется,
    # и он обязан УВИДЕТЬ чужую строку row-y (факт с сервера), не только свою row-x.
    seen_row_y = []

    def is_dup_z(current: str) -> bool:
        seen_row_y.append("row-y" in current)
        return "row-z" in current.splitlines()

    def render_z(current: str) -> str:
        return current + "row-z\n"

    assert dbw.append_and_push(str(writer_a), "data/writer-test", "data.txt",
                               COMMIT_IDENTITY, "row z", is_dup_z, render_z) is True
    assert True in seen_row_y, "писатель не увидел чужой коммит с сервера после гонки"

    check = tmp_path / "check"
    subprocess.run(["git", "clone", "--quiet", "--branch", "data/writer-test",
                    str(bare), str(check)], check=True)
    final = (check / "data.txt").read_text(encoding="utf-8").splitlines()
    assert final == ["row-x", "row-y", "row-z"]


def test_append_and_push_raises_loud_on_auth_rejection(tmp_path):
    """Отказ авторизации (403-подобный `pre-receive`, дословно похожий на
    реальный GitHub) обязан упасть ГРОМКО, а не тихо решить «кто-то другой
    уже записал» (#882, живой инцидент — репозиторий существовал, ветка
    данных нет, push падал 403, и старый код молчал)."""
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "--quiet", "--bare", "--initial-branch=main", str(bare)],
                   check=True)
    hooks_dir = bare / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / "pre-receive"
    hook.write_text(
        "#!/bin/sh\n"
        "while read old new ref; do\n"
        "  case \"$ref\" in\n"
        "    refs/heads/data/*)\n"
        "      echo \"remote: Permission to owner/repo.git denied to actor.\" >&2\n"
        "      echo \"remote: error: GH006: Protected branch update failed\" >&2\n"
        "      exit 1\n"
        "      ;;\n"
        "  esac\n"
        "done\n"
        "exit 0\n",
        encoding="utf-8", newline="\n",
    )
    hook.chmod(0o755)

    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "--quiet", str(bare), str(seed)], check=True)
    subprocess.run(["git", "-C", str(seed), "checkout", "--quiet", "-B", "main"], check=True)
    (seed / "README.md").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
    subprocess.run(["git", "-C", str(seed), *COMMIT_IDENTITY, "commit", "--quiet", "-m", "seed"],
                   check=True)
    subprocess.run(["git", "-C", str(seed), "push", "--quiet", "origin", "main"], check=True)

    work = tmp_path / "work"
    subprocess.run(["git", "clone", "--quiet", str(bare), str(work)], check=True)
    subprocess.run(["git", "-C", str(work), "checkout", "--quiet", "-B", "data/denied",
                    "origin/main"], check=True)

    is_dup, render = _line_writer("row-1")
    # До фикса #882 это место возвращало False («уже записано параллельным
    # прогоном») вместо громкого падения — RuntimeError здесь и есть
    # доказательство фикса, не тихий возврат.
    with pytest.raises(RuntimeError, match="auth"):
        dbw.append_and_push(str(work), "data/denied", "data.txt", COMMIT_IDENTITY,
                            "row 1", is_dup, render)
    # Ветка так и не появилась на origin — фактически ничего не записано.
    assert not dbw.branch_exists_on_origin(str(work), "data/denied")


def test_clone_data_branch_first_creation_path(tmp_path):
    """Ветки данных на origin нет вовсе — clone_data_branch обязан завести её
    от main, не упасть на отсутствующем origin/<br> (#882, требование 3)."""
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "--quiet", "--bare", "--initial-branch=main", str(bare)],
                   check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "--quiet", str(bare), str(seed)], check=True)
    subprocess.run(["git", "-C", str(seed), "checkout", "--quiet", "-B", "main"], check=True)
    (seed / "README.md").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
    subprocess.run(["git", "-C", str(seed), *COMMIT_IDENTITY, "commit", "--quiet", "-m", "seed"],
                   check=True)
    subprocess.run(["git", "-C", str(seed), "push", "--quiet", "origin", "main"], check=True)

    workdir = tmp_path / "fresh"
    dbw.clone_data_branch(str(bare), str(workdir), "data/brand-new")
    current = subprocess.run(["git", "-C", str(workdir), "rev-parse", "--abbrev-ref", "HEAD"],
                             capture_output=True, text=True, encoding="utf-8", check=True)
    assert current.stdout.strip() == "data/brand-new"

    is_dup, render = _line_writer("row-1")
    assert dbw.append_and_push(str(workdir), "data/brand-new", "data.txt", COMMIT_IDENTITY,
                               "row 1", is_dup, render) is True
    check = tmp_path / "check"
    subprocess.run(["git", "clone", "--quiet", "--branch", "data/brand-new", str(bare),
                    str(check)], check=True)
    assert (check / "data.txt").read_text(encoding="utf-8") == "row-1\n"
