#!/usr/bin/env python3
"""Гвардия «дедуп issue-create не выключается молча» (issue #1395).

Класс: «не смог посмотреть» засчитывалось как «прошло». Прежняя редакция
`scripts/gh/issue-create` знала ОДИН источник репозитория — `gh repo view`, —
а он ходит в GraphQL и в агентских каналах штатно отвечает 403; сверка на
дубли при этом молча выключалась, печатая `::warning::`. Репозиторий уже
признал этот класс дефектом в другом месте: гвардия пуша (#1096) при
недоступном `gh` БЛОКИРУЕТ и требует осознанной расписки.

Цена измерена, а не предположена: #1385 и #1386 — байт-в-байт одинаковые
задачи (sha256 тел совпадает), созданные с разницей в 7 секунд, обе висели в
пуле открытыми.

Решение — ОБЪЕДИНЕНИЕ обоих вариантов, названных в #1395, а не выбор одного:
сначала снимается ПРИЧИНА (три источника репозитория вместо одного), и лишь
если не сработал ни один — ставится рубеж с газом (`--dedup-skip-ack`,
причина уходит в тело issue). «Только рубеж» тормозил бы штатный агентский
путь; «только источники» оставляли бы молчаливый пропуск там, где не
сработало ничто. Объединение строго лучше любого из двух по отдельности,
поэтому выбор владельцу не выносился.

Стенд ПОВЕДЕНЧЕСКИЙ: настоящий bash запускает настоящий скрипт; недоступность
`gh repo view` воспроизводится настоящим ненулевым кодом возврата подменённого
в PATH `gh` (подменяется КЛИЕНТ GitHub, которому в тесте взяться неоткуда, —
не проверяемый скрипт и не его логика); настоящий `git init` даёт настоящий
remote. Тест на pytest, а не на bash: каталог гвардий (#749) — единственный
путь регистрации, а гейт мутационного доказательства исполняет только форму
`python -m pytest <цель> -q` (`mutation_claim.py::TEST_CMD_RE`).

Запуск: python -m pytest scripts/lib/test_issue_create_repo_resolution.py -q
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import json
import os
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = ROOT / "scripts" / "gh" / "issue-create"

# Дословные заголовки живого инцидента 2026-09-06 (#562/#564) — та же фикстура,
# что у соседних гвардий issue-create: дедуп проверяется на реально
# измеренном дубле, не на придуманной паре строк.
TITLE_562 = ("deploy-dsh-edge: автооткат (#549) оставляет рассинхрон версий — "
             "следующий wrangler secret put падает")
TITLE_564 = ("deploy-dsh-edge: автооткат (#549) блокирует следующий деплой — "
             "wrangler secret put падает VERSION_NOT_DEPLOYED")

VALID_BODY = "### Чем блокируется\nничем\n"
NOT_PROCESS_ACK = ["--not-process-ack", "не про приоритет, тест источников репозитория #1395"]

FAKE_GH = '''#!/usr/bin/env bash
if [ "$1" = "repo" ] && [ "$2" = "view" ]; then
  echo "GraphQL is not available from this session (HTTP 403)" >&2
  exit 1
fi
if [ "$1" = "issue" ] && [ "$2" = "create" ]; then
  echo called >"$FAKE_GH_MARKER"
  args=("$@")
  for idx in "${!args[@]}"; do
    if [ "${args[$idx]}" = "--body" ]; then
      next=$((idx + 1)); printf '%s' "${args[$next]}" >"$FAKE_GH_BODY"
    fi
    if [ "${args[$idx]}" = "--body-file" ]; then
      next=$((idx + 1)); cat "${args[$next]}" >"$FAKE_GH_BODY"
    fi
  done
  echo "https://github.com/o/r/issues/999"
  exit 0
fi
echo "unexpected gh call: $*" >&2
exit 1
'''

needs_bash = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("git") is None,
                                reason="нужны настоящие bash и git")


class Stand:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.marker = tmp_path / "gh-issue-create-called"
        self.created_body = tmp_path / "created-body"
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text(FAKE_GH, encoding="utf-8")
        gh.chmod(0o755)
        fixture = tmp_path / "open-tasks.json"
        fixture.write_text(json.dumps(
            [{"number": 562, "title": TITLE_562, "url": "https://github.com/o/r/issues/562"}],
            ensure_ascii=False), encoding="utf-8")
        priority = tmp_path / "priority.json"
        priority.write_text("[]", encoding="utf-8")
        self.env = dict(os.environ)
        self.env.pop("GITHUB_REPOSITORY", None)
        self.env.update({
            "PATH": f"{bin_dir}{os.pathsep}{self.env['PATH']}",
            "DUPLICATE_GUARD_FIXTURE": str(fixture),
            "PRIORITY_TOP_FIXTURE": str(priority),
            "FAKE_GH_MARKER": str(self.marker),
            "FAKE_GH_BODY": str(self.created_body),
        })
        # Каталог БЕЗ git-репозитория: чтобы источники проверялись по одному,
        # а не вперемешку с origin рабочего дерева самого харнеса.
        self.no_git = tmp_path / "no-git"
        self.no_git.mkdir()

    def git_repo(self, url="https://github.com/o/r.git"):
        path = self.tmp / "gitrepo"
        path.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=path, check=True)
        subprocess.run(["git", "remote", "add", "origin", url], cwd=path, check=True)
        return path

    def run(self, *extra, cwd=None, repo_env=None):
        env = dict(self.env)
        if repo_env is not None:
            env["GITHUB_REPOSITORY"] = repo_env
        return subprocess.run(
            ["bash", str(SCRIPT), "--title", TITLE_564, "--body", VALID_BODY,
             "--label", "task", *NOT_PROCESS_ACK, *extra],
            cwd=str(cwd or self.no_git), env=env,
            capture_output=True, text=True, encoding="utf-8")

    @property
    def gh_create_called(self):
        return self.marker.exists()


@pytest.fixture
def stand(tmp_path):
    return Stand(tmp_path)


@needs_bash
def test_env_variable_saves_the_dedup_when_gh_repo_view_is_denied(stand):
    """Главная сцена #1395: ровно та среда, где сверка молча выключалась —
    `gh repo view` отвечает 403. Репозиторий берётся из GITHUB_REPOSITORY, и
    дубль ловится, а не проходит."""
    result = stand.run(repo_env="o/r")
    out = result.stdout + result.stderr

    assert result.returncode != 0, out
    assert not stand.gh_create_called, "gh issue create вызван при найденном дубле"
    assert "562" in out, out
    assert "GITHUB_REPOSITORY" in out, "источник репозитория обязан быть назван вслух: " + out


@needs_bash
def test_git_remote_saves_the_dedup_when_nothing_else_answers(stand):
    """Третий источник — настоящий `git remote get-url origin` настоящего
    репозитория, созданного `git init`, а не разобранная строка из фикстуры."""
    result = stand.run(cwd=stand.git_repo())
    out = result.stdout + result.stderr

    assert result.returncode != 0, out
    assert "git remote" in out, "источник репозитория обязан быть назван вслух: " + out
    assert "562" in out, out


@needs_bash
def test_no_source_at_all_refuses_instead_of_skipping_silently(stand):
    """Сам класс: не сработал НИ ОДИН источник — значит сверить было нечем, и
    это НЕ «дублей нет». Прежняя редакция здесь печатала `::warning::` и
    создавала issue."""
    result = stand.run()
    out = result.stdout + result.stderr

    assert result.returncode != 0, out
    assert not stand.gh_create_called, "issue создана при невозможности сверки — это и есть класс #1395"
    assert "dedup-skip-ack" in out, "тормоз без газа: расписка в отказе не названа: " + out


@needs_bash
def test_explicit_ack_creates_the_issue_and_records_why_in_its_body(stand):
    """Газ существует и ведёт куда обещано: расписка принимается, а причина
    уходит в ТЕЛО issue, а не тонет в одном терминале — следующий читатель
    задачи должен знать, что сверки не было."""
    result = stand.run("--dedup-skip-ack", "сверил руками постраничным обходом открытых задач")
    out = result.stdout + result.stderr

    assert result.returncode == 0, out
    assert stand.gh_create_called, out
    body = stand.created_body.read_text(encoding="utf-8")
    assert "сверил руками постраничным обходом" in body, body


@needs_bash
def test_the_note_does_not_push_the_dependency_declaration_out_of_the_tail(stand):
    """Класс #720/#804: приписка не имеет права оттеснить «Чем блокируется» —
    гейт объявления связи отработал ДО приписки, и созданная issue обязана
    по-прежнему его нести. Одно место правды на обе расписки
    (`append_body_note`) проверяется именно здесь."""
    stand.run("--dedup-skip-ack", "сверка невозможна, проверено руками")
    body = stand.created_body.read_text(encoding="utf-8")
    tail = "\n".join(body.splitlines()[-5:])

    assert "ничем" in tail, body
