#!/usr/bin/env python3
"""Гвардия worktree-cleanup (#891, расширена #1250): безопасность удаления
мёртвых деревьев, включая squash-safe признак сохранности работы.

Раньше гвардия проверяла ТЕКСТ исходника (наличие имён функций
`check_dirty`/`check_unpushed_commits` в файле) — переименование функции
красило её ложно-зелёной, а вырезание тела проверки при сохранённом имени
проходило бы молча. Здесь — только ПОВЕДЕНИЕ на настоящем временном
git-репозитории (tempfile + `git init` + реальный `git worktree add`), без
чтения исходника.

Живой замер (#891) вскрыл, что сигнал "ветка есть на origin" не работает в
ЭТОМ репозитории: оркестратор сливает PR, но не удаляет ветку — 127 из 128
реальных деревьев показывали "ветка ещё на origin", включая PR, смердженные
месяцами ранее. Поэтому здесь тестируется актуальный сигнал — статус PR
(`get_pr_status`, `gh pr list --head <ветка>` — точное совпадение имени
ветки, не `--search "head:<префикс>"`, который матчит подстрокой и на живом
прогоне вернул 30 посторонних PR на запрос по одной ветке).

Живой замер (#1250) вскрыл ВТОРОЙ класс, в три раунда (дословно — докстринг
`check_unpushed_commits`, не пересказ здесь): `git merge-base --is-ancestor
HEAD origin/main` НИКОГДА не признаёт работу слитой после squash-слияния
(живое дерево `1211-stale-blocked-prose`, PR #1216). Раунд 1 (сравнение с
main) и раунд 2 (плюс сравнение с живым апстримом) на живых 73-76 деревьях
дали `Removed: 0` ОБА раза — main и апстрим постоянно копят чужие/довесочные
изменения, полное сравнение дерева не масштабируется. Раунд 3 (этот код)
добавляет статус PR ПОСЛЕДНИМ рубежом, когда оба сравнения содержимого
разошлись — единственная комбинация, реально снимающая деревья на живых
данных (подтверждено: #1109 PR #1140 MERGED, #1027 PR #1030 MERGED — оба
диффят и от апстрима, и от main, но снимаются через доверие статусу PR).
Сценарии 9/10a/10b здесь — прямое воспроизведение (реальный `git merge
--squash`, реальный force-push переписанной ветки), не пересказ.

Сценарии (все — обязательные условия неудаления, AGENTS.md «Fail loud»):
  1. Дерево с незакоммиченным файлом — снятие ОТКАЗАНО (и с --force тоже),
     сообщение называет газ (закоммить/отбросить/.worktree-keep).
  2. Дерево с локальным коммитом, которого нет НИГДЕ (апстрим не совпадает,
     PR ещё open, не merged) — ОТКАЗАНО, `check_unpushed_commits` возвращает
     `violation`.
  3. Дерево, у которого PR ещё open — ОТКАЗАНО.
  4. Дерево младше retention — ОТКАЗАНО.
  5. Чистое дерево, PR которого merged/closed, апстрим синхронен, старше
     retention — СНЯТО, и физически исчезает с диска.
  6. Апстрим удалён, но ветка тривиально совпадает с main (условие 2 честно
     говорит ok() уже на ярусе сравнения содержимого, до статуса PR) —
     блокирует условие 3: статус PR определить не удалось (gh недоступен/PR
     не найден) — ОТКАЗАНО (fail loud, не молчаливое разрешение), `unknown_pr`.
  7. Два PR на одну ветку (closed/merged старый + open новый, живой сценарий
     этого репозитория — ветка `agent/<N>-<slug>` переиспользуется при
     перезапуске задачи) — `open` обязан побеждать вне зависимости от
     порядка записей в ответе `gh pr list` (находка ревью PR #893, второй
     раунд). Реальный сетевой вызов через фейковый `gh` на PATH
     (`_install_fake_gh`), не монкипатч `get_pr_status` целиком — единственный
     сетевой компонент сигнала (`_load_pr_status_cache`) иначе не тестируется
     вообще.
  8. Файл-маркер `.worktree-keep` в корне дерева — снятие ОТКАЗАНО даже для
     иначе безопасного к удалению дерева, и с --force тоже (известный хвост
     #891: ручное исключение защищённых деревьев при живом прогоне-замере
     не имело объявленного механизма).
  9. Squash-safe признак, ярус 1 (#1250): апстрим переписан (реальный
     force-push амендированного коммита), классика (`rev-list --count
     @{u}..`) отдаёт >0 НАВСЕГДА — но содержимое HEAD равно ЖИВОМУ апстриму
     ⇒ условие 2 говорит ok() уже на ярусе 1, статус PR не понадобился ⇒
     дерево СНИМАЕТСЯ (условие 3 тоже пройдено, PR смёржен).
  10a. Содержимое разошлось И с апстримом, И с main, а PR доказанно НЕ
      merged (`closed`) — ОТКАЗАНО, `unpushed`: ярус 3 (доверие статусу PR)
      честно отказывает, недоказанная сохранность не выдаётся за доказанную.
  10b. ИМЕННО НАЗВАННЫЙ остаточный риск (issue #1250, раунд 3): follow-up
      коммит ПОСЛЕ того, как PR уже merged, никогда не запушенный никуда —
      ПРИНЯТ как известный компромисс (см. докстринг `check_unpushed_commits`,
      "ЧЕСТНО НАЗВАННЫЙ ОСТАТОЧНЫЙ РИСК") и СНИМАЕТСЯ. Тест фиксирует это
      поведение намеренно, не молчаливым регрессом.
  11. `--force` не снимает проверку сохранности работы (условие 2, включая
      ярус 3 внутри неё) — только ОТДЕЛЬНЫЙ гейт "PR open" (условие 3).
      Апстрим удалён, статус PR не определён (честный отказ локального
      bare-репозитория) — с `--force` дерево всё равно остаётся `unknown_work`,
      потому что условие 2 вызывает `get_pr_status` самостоятельно и не
      читает `self.force`.
  12. Каталог под `.claude/worktrees/` без `.git` (не зарегистрирован в `git
      worktree list`) — не удаляется, не защищается, но ОБЯЗАН появиться в
      отчёте (`scan_orphan_directories`/`stats['orphan_dirs']`), не молчаливо
      игнорируется.

get_pr_status монкипатчится на уровне класса для сценариев, где сетевой
`gh`-вызов не имеет отношения к тому, что сценарий проверяет (dirty/young/
squash-safe/keep-marker); сценарии 3, 6, 11 используют реальный вызов (fake
gh или честный отказ локального bare-репозитория) сознательно; сценарий 7
использует фейковый `gh` на PATH — см. комментарии внутри.

Retention/«текущее время» инъецируются параметром (`retention_hours`,
`now_ts`) — тесты не зависят от системных часов и скорости выполнения
(класс «тесты-бомбы», AGENTS.md).

Доказательство мутацией (issue #1250, ручной прогон — дословный вывод живёт
в отчёте PR, не по памяти):
  - вернуть `check_unpushed_commits` к старому поведению (использовать
    ТОЛЬКО `git rev-list --count @{u}..`/`git merge-base --is-ancestor HEAD
    origin/main`, убрав все три яруса squash-safe сравнения) красит
    `test_squash_merged_worktree_with_rewritten_upstream_is_removed` в RED
    (дерево навсегда числится "unpushed", хотя PR давно смёржен squash'ем);
    вернуть squash-safe путь — GREEN.
  - убрать ярус 3 (доверие статусу PR, оставив только сравнение содержимого
    с апстримом/main) красит `test_merged_pr_with_diverged_content_is_trusted_named_risk`
    в RED (живой замер: без яруса 3 `Removed: 0` на реальных 73-76
    деревьях, включая заведомо merged #1109/#1027) — вернуть ярус 3 — GREEN.
  - закомментировать тело проверки `check_dirty` внутри `can_remove_worktree`
    (оставив имя функции нетронутым) красит
    `test_dirty_worktree_is_refused_even_with_force` в RED; вернуть тело —
    GREEN.
  - заменить агрегацию `_load_pr_status_cache` на "последний в JSON
    выигрывает" (`cache[ref] = state.lower()` без сверки приоритета) красит
    `test_duplicate_pr_per_branch_open_wins_open_listed_first` в RED (кэш
    даёт `closed` вместо `open`, когда open — НЕ последняя запись); вернуть
    приоритет — GREEN.
  - заменить тело `has_keep_marker` на `return False` красит
    `test_keep_marker_protects_otherwise_removable_worktree` в RED (дерево с
    маркером отказывается не по причине "protected", а падает в
    `check_dirty`, потому что сам untracked-маркер и есть незакоммиченное
    изменение, — но проверка ловит ИМЕННО заявленную причину отказа, не
    любой отказ); вернуть тело — GREEN.

Запуск: python -m pytest scripts/lib/test_worktree_cleanup_guard.py -q
"""

import os
import subprocess
import sys

import pytest

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "git" / "worktree-cleanup.py"

_wc_spec = importlib.util.spec_from_file_location("worktree_cleanup", _SCRIPT_PATH)
worktree_cleanup = importlib.util.module_from_spec(_wc_spec)
_wc_spec.loader.exec_module(worktree_cleanup)  # type: ignore[union-attr]

WorktreeAnalyzer = worktree_cleanup.WorktreeAnalyzer
check_result = worktree_cleanup.check_result

RETENTION_HOURS = 1.0


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {cwd}: {result.stdout}\n{result.stderr}"
        )
    return result


def _setup_repo(tmp_path: Path) -> Path:
    """Завести bare-репозиторий 'origin' и рабочий клон с одним коммитом на main."""
    bare = tmp_path / "origin.git"
    work = tmp_path / "repo"
    _git(tmp_path, "init", "--bare", str(bare))
    _git(tmp_path, "clone", str(bare), str(work))
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "Test")
    (work / "README.md").write_text("init\n", encoding="utf-8")
    _git(work, "add", "README.md")
    _git(work, "commit", "-m", "init")
    _git(work, "push", "origin", "HEAD:main")
    _git(work, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    return work


def _add_worktree(work: Path, name: str, branch: str, push: bool = True) -> Path:
    """Создать worktree под .claude/worktrees/<name> на новой ветке от origin/main."""
    wt_dir = work / ".claude" / "worktrees" / name
    wt_dir.parent.mkdir(parents=True, exist_ok=True)
    _git(work, "worktree", "add", "-b", branch, str(wt_dir), "origin/main")
    if push:
        _git(work, "push", "origin", branch)
        _git(wt_dir, "branch", f"--set-upstream-to=origin/{branch}", branch)
    return wt_dir


def _delete_remote_branch(work: Path, branch: str) -> None:
    _git(work, "push", "origin", "--delete", branch)
    _git(work, "fetch", "origin", "--prune")


def _mtime_now_ts(path: Path, offset_hours: float) -> float:
    """now_ts, вычисленный ОТНОСИТЕЛЬНО реального mtime дерева, а не от
    time.time() напрямую — инъекция, не системные часы (класс «тесты-бомбы»)."""
    import os

    mtime = os.stat(path).st_mtime
    return mtime + offset_hours * 3600


def _worktree_info(work: Path, branch: str) -> dict:
    """Вернуть {'path', 'branch'} — ровно то, что отдаёт get_worktrees() для этой ветки."""
    for info in WorktreeAnalyzer(str(work)).get_worktrees():
        if info.get("branch") == f"refs/heads/{branch}":
            return info
    raise AssertionError(f"worktree на ветке {branch} не найден в git worktree list")


def _patch_pr_status(monkeypatch, value):
    """PR-статус недетерминирован без реального GitHub-репозитория (сетевой
    gh-вызов) — монкипатчим на уровне класса там, где сценарий проверяет НЕ
    его, а другой гейт (dirty/unpushed/retention)."""
    monkeypatch.setattr(WorktreeAnalyzer, "get_pr_status", lambda self, branch: value)


def _install_fake_gh(monkeypatch, tmp_path: Path, pr_list_json: str) -> None:
    """Подложить фейковый `gh`, отвечающий на `gh pr list --state all
    --json headRefName,state --limit 2000` заданным JSON — прод-форма
    ответа `gh pr list`, не пересказ (AGENTS.md «Тест кормит прод-форму
    данных»). Проверяет РЕАЛЬНЫЙ сетевой путь `_load_pr_status_cache`
    (находка ревью PR #893, второй раунд: «единственный сетевой компонент
    сигнала не тестируется ничем» — раньше все сценарии монкипатчили
    `get_pr_status` целиком).

    Два файла, не один — тот же класс, что уже ловил этот репозиторий
    (issue упомянута в задании #891): `run_cmd` вызывает `gh` через
    `subprocess.run(..., shell=True)`, на Windows это cmd.exe, который
    резолвит PATH по PATHEXT (нужен `.cmd`/`.exe`/`.bat`, голый `gh` без
    расширения не находится); на POSIX shell=True это `/bin/sh`, резолвящий
    по биту исполнения независимо от расширения. `gh` (POSIX, exec-бит) и
    `gh.cmd` (Windows) — два тонких враппера ОДНОГО python-скрипта, чтобы
    логика ответа жила в одном месте."""
    bin_dir = tmp_path / "fake-gh-bin"
    bin_dir.mkdir(exist_ok=True)

    payload_file = bin_dir / "pr_list_payload.json"
    payload_file.write_text(pr_list_json, encoding="utf-8")

    responder = bin_dir / "fake_gh_responder.py"
    responder.write_text(
        "import sys, pathlib\n"
        "payload = pathlib.Path(__file__).with_name('pr_list_payload.json')\n"
        "if 'pr' in sys.argv and 'list' in sys.argv:\n"
        "    sys.stdout.write(payload.read_text(encoding='utf-8'))\n"
        "    sys.exit(0)\n"
        "sys.stderr.write('fake gh: неизвестная команда ' + ' '.join(sys.argv[1:]))\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )

    fake_gh_posix = bin_dir / "gh"
    fake_gh_posix.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, pathlib, runpy\n"
        "runpy.run_path(str(pathlib.Path(__file__).with_name('fake_gh_responder.py')), run_name='__main__')\n",
        encoding="utf-8",
    )
    fake_gh_posix.chmod(fake_gh_posix.stat().st_mode | 0o111)

    fake_gh_cmd = bin_dir / "gh.cmd"
    fake_gh_cmd.write_text(
        f'@echo off\r\n{sys.executable} "%~dp0fake_gh_responder.py" %*\r\n',
        encoding="utf-8",
    )

    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")


# ── Сценарий 1: незакоммиченные изменения ──────────────────────────────────

def test_dirty_worktree_is_refused_even_with_force(tmp_path, monkeypatch):
    _patch_pr_status(monkeypatch, "merged")  # PR-гейт заведомо не мешает — изолируем dirty
    work = _setup_repo(tmp_path)
    branch = "agent/1-dirty"
    wt_dir = _add_worktree(work, "1-dirty", branch)
    (wt_dir / "uncommitted.txt").write_text("x", encoding="utf-8")

    now_ts = _mtime_now_ts(wt_dir, RETENTION_HOURS * 10)  # заведомо старше retention
    info = _worktree_info(work, branch)

    analyzer = WorktreeAnalyzer(str(work), force=False, retention_hours=RETENTION_HOURS, now_ts=now_ts)
    assert analyzer.check_dirty(str(wt_dir)) is True

    can, code, reason = analyzer.can_remove_worktree(info)
    assert can is False, reason
    assert code == "dirty"
    # газ обязан быть названным, не просто "нельзя" (AGENTS.md «тормоз без газа»)
    assert "закоммить" in reason.lower() or "commit" in reason.lower()

    analyzer_force = WorktreeAnalyzer(str(work), force=True, retention_hours=RETENTION_HOURS, now_ts=now_ts)
    can_force, code_force, reason_force = analyzer_force.can_remove_worktree(info)
    assert can_force is False, f"--force не обязан снимать грязную проверку: {reason_force}"
    assert code_force == "dirty"

    stats = analyzer.analyze_and_cleanup()
    assert stats["removed"] == 0
    assert wt_dir.exists(), "грязное дерево не должно исчезать с диска"


# ── Сценарий 2: локальный коммит, которого нет НИГДЕ ────────────────────────

def test_unpushed_commit_worktree_is_refused(tmp_path, monkeypatch):
    _patch_pr_status(monkeypatch, "open")  # реалистичный статус для работы
    # в процессе — НЕ "merged": содержимое отличается от main (ярусы 1/2
    # диффа отказывают), а PR ещё не смёржен, поэтому ярус 3 (доверие
    # статусу PR, issue #1250 раунд 3) тоже честно отказывает.
    work = _setup_repo(tmp_path)
    branch = "agent/2-unpushed"
    wt_dir = _add_worktree(work, "2-unpushed", branch)

    (wt_dir / "extra.txt").write_text("extra\n", encoding="utf-8")
    _git(wt_dir, "add", "extra.txt")
    _git(wt_dir, "commit", "-m", "local only, never pushed")

    now_ts = _mtime_now_ts(wt_dir, RETENTION_HOURS * 10)
    info = _worktree_info(work, branch)

    analyzer = WorktreeAnalyzer(str(work), retention_hours=RETENTION_HOURS, now_ts=now_ts)
    work_result = analyzer.check_unpushed_commits(str(wt_dir), branch)
    assert work_result.status == check_result.STATUS_VIOLATION, work_result

    can, code, reason = analyzer.can_remove_worktree(info)
    assert can is False, reason
    assert code == "unpushed"
    assert "push" in reason.lower() or "merge" in reason.lower()  # газ назван

    stats = analyzer.analyze_and_cleanup()
    assert stats["removed"] == 0
    assert wt_dir.exists(), "дерево с непушенным коммитом не должно исчезать с диска"


# ── Сценарий 3: PR ещё open ──────────────────────────────────────────────

def test_open_pr_worktree_is_refused(tmp_path, monkeypatch):
    _patch_pr_status(monkeypatch, "open")
    work = _setup_repo(tmp_path)
    branch = "agent/3-open"
    wt_dir = _add_worktree(work, "3-open", branch)
    _delete_remote_branch(work, branch)  # ветка удалена — но PR всё равно open

    now_ts = _mtime_now_ts(wt_dir, RETENTION_HOURS * 10)
    info = _worktree_info(work, branch)

    analyzer = WorktreeAnalyzer(str(work), retention_hours=RETENTION_HOURS, now_ts=now_ts)
    can, code, reason = analyzer.can_remove_worktree(info)
    assert can is False, reason
    assert code == "open_pr"
    assert "open" in reason.lower()

    stats = analyzer.analyze_and_cleanup()
    assert stats["removed"] == 0
    assert wt_dir.exists(), "дерево с открытым PR не должно исчезать с диска"


# ── Сценарий 4: дерево младше retention ────────────────────────────────────

def test_young_worktree_is_refused(tmp_path, monkeypatch):
    _patch_pr_status(monkeypatch, "merged")  # изолируем retention-гейт
    work = _setup_repo(tmp_path)
    branch = "agent/4-young"
    wt_dir = _add_worktree(work, "4-young", branch)

    # Прошла только половина окна retention — заведомо младше, без зависимости
    # от реальной скорости выполнения теста.
    now_ts = _mtime_now_ts(wt_dir, RETENTION_HOURS * 0.5)
    info = _worktree_info(work, branch)

    analyzer = WorktreeAnalyzer(str(work), retention_hours=RETENTION_HOURS, now_ts=now_ts)
    age = analyzer.get_worktree_age_hours(str(wt_dir))
    assert age < RETENTION_HOURS

    can, code, reason = analyzer.can_remove_worktree(info)
    assert can is False, reason
    assert code == "young"

    stats = analyzer.analyze_and_cleanup()
    assert stats["removed"] == 0
    assert wt_dir.exists(), "молодое дерево не должно исчезать с диска"


# ── Сценарий 5: чистое дерево, PR merged, апстрим синхронен, старше retention

def test_clean_merged_worktree_is_removed_from_disk(tmp_path, monkeypatch):
    _patch_pr_status(monkeypatch, "merged")
    work = _setup_repo(tmp_path)
    branch = "agent/5-merged"
    wt_dir = _add_worktree(work, "5-merged", branch)
    _delete_remote_branch(work, branch)

    now_ts = _mtime_now_ts(wt_dir, RETENTION_HOURS * 10)
    info = _worktree_info(work, branch)

    analyzer = WorktreeAnalyzer(str(work), retention_hours=RETENTION_HOURS, now_ts=now_ts)
    can, code, reason = analyzer.can_remove_worktree(info)
    assert can is True, reason
    assert code == "ok"

    stats = analyzer.analyze_and_cleanup()
    assert stats["removed"] == 1, stats
    assert not wt_dir.exists(), "безопасное дерево обязано физически исчезнуть с диска"

    remaining = _git(work, "worktree", "list", "--porcelain").stdout
    assert str(wt_dir) not in remaining, "git тоже не должен помнить снятое дерево"


# ── Сценарий 6: статус PR не определён — отказ, не молчаливое разрешение ───

def test_unknown_pr_status_is_refused(tmp_path):
    """Без монкипатча: origin — локальный bare-репозиторий, не GitHub, gh
    честно не может определить владельца/репозиторий и возвращает ошибку
    быстро одним пакетным вызовом (не сеть, не таймаут — проверено вручную:
    ~0.8с). get_pr_status обязан вернуть None, а can_remove_worktree —
    отказать, а не молча разрешить снятие (fail loud, не silent-wrong,
    AGENTS.md)."""
    work = _setup_repo(tmp_path)
    branch = "agent/6-unknown"
    wt_dir = _add_worktree(work, "6-unknown", branch)
    _delete_remote_branch(work, branch)

    now_ts = _mtime_now_ts(wt_dir, RETENTION_HOURS * 10)
    info = _worktree_info(work, branch)

    analyzer = WorktreeAnalyzer(str(work), retention_hours=RETENTION_HOURS, now_ts=now_ts)
    assert analyzer.get_pr_status(branch) is None

    # Апстрим удалён, но у этой ветки НЕТ локальных коммитов сверх main
    # (`_add_worktree` не добавляет коммитов) -> содержимое HEAD совпадает с
    # origin/main -> check_unpushed_commits(condition 2) честно говорит ok()
    # (это не зависит от статуса PR, см. докстринг файла). Блокирует
    # снятие условие 3 (статус PR) — третье состояние (unknown), не
    # violation: причина "не удалось доказать", не "доказано, что потеряно".
    can, code, reason = analyzer.can_remove_worktree(info)
    assert can is False, reason
    assert code == "unknown_pr"
    assert "determine" in reason.lower()

    stats = analyzer.analyze_and_cleanup()
    assert stats["removed"] == 0
    assert wt_dir.exists(), "при неопределённом статусе PR дерево не должно исчезать"


# ── Сценарий 7: дубль PR на одну ветку — open обязан побеждать ─────────────
# Живой сценарий этого репозитория (не гипотетический, находка ревью PR
# #893, второй раунд): ветка `agent/<N>-<slug>` детерминирована номером
# задачи и переиспользуется при перезапуске задачи после закрытого PR —
# `gh pr list` может вернуть на одну ветку и старый closed, и новый open PR.
# Реальный сетевой вызов (фейковый `gh` на PATH), не монкипатч
# `get_pr_status` целиком — единственный сетевой компонент сигнала обязан
# быть покрыт хоть одним тестом (находка ревью).

def test_duplicate_pr_per_branch_open_wins_closed_listed_first(tmp_path, monkeypatch):
    _install_fake_gh(
        monkeypatch,
        tmp_path,
        '[{"headRefName": "agent/7-dup", "state": "CLOSED"},'
        ' {"headRefName": "agent/7-dup", "state": "OPEN"}]',
    )
    analyzer = WorktreeAnalyzer(str(tmp_path))
    assert analyzer.get_pr_status("agent/7-dup") == "open"


def test_duplicate_pr_per_branch_open_wins_open_listed_first(tmp_path, monkeypatch):
    """Тот же дубль, обратный порядок записей — доказывает, что победа
    open не завязана на позицию в JSON (не "последний в списке", а
    приоритет статуса)."""
    _install_fake_gh(
        monkeypatch,
        tmp_path,
        '[{"headRefName": "agent/7-dup", "state": "OPEN"},'
        ' {"headRefName": "agent/7-dup", "state": "CLOSED"}]',
    )
    analyzer = WorktreeAnalyzer(str(tmp_path))
    assert analyzer.get_pr_status("agent/7-dup") == "open"


def test_open_pr_duplicate_keeps_worktree_end_to_end(tmp_path, monkeypatch):
    """Сквозной прогон сценария 3, но статус берётся РЕАЛЬНЫМ вызовом
    (фейковый gh), а не монкипатчем `get_pr_status` — дерево с дублем PR
    (closed + open) на одну ветку обязано остаться на диске."""
    _install_fake_gh(
        monkeypatch,
        tmp_path,
        '[{"headRefName": "agent/7-dup", "state": "CLOSED"},'
        ' {"headRefName": "agent/7-dup", "state": "OPEN"}]',
    )
    work = _setup_repo(tmp_path)
    branch = "agent/7-dup"
    wt_dir = _add_worktree(work, "7-dup", branch)

    now_ts = _mtime_now_ts(wt_dir, RETENTION_HOURS * 10)
    info = _worktree_info(work, branch)

    analyzer = WorktreeAnalyzer(str(work), retention_hours=RETENTION_HOURS, now_ts=now_ts)
    can, code, reason = analyzer.can_remove_worktree(info)
    assert can is False, reason
    assert code == "open_pr"
    assert "open" in reason.lower()

    stats = analyzer.analyze_and_cleanup()
    assert stats["removed"] == 0
    assert wt_dir.exists(), "дерево с дублем PR (closed+open) не должно исчезать с диска"


# ── Сценарий 8: файл-маркер защищает дерево, которое иначе снялось бы ─────
# Известный хвост #891 (не находка ревью, отдельно названный в задаче):
# при живом прогоне-замере оператору пришлось РУКАМИ исключить деревья
# 749-ci-guard-catalog и собственное дерево агента — продакшн-скрипт не
# нёс объявленного способа исключения вовсе. `.worktree-keep` — тот способ.

def test_keep_marker_protects_otherwise_removable_worktree(tmp_path, monkeypatch):
    _patch_pr_status(monkeypatch, "merged")
    work = _setup_repo(tmp_path)
    branch = "agent/8-protected"
    wt_dir = _add_worktree(work, "8-protected", branch)
    (wt_dir / ".worktree-keep").write_text("", encoding="utf-8")
    _delete_remote_branch(work, branch)

    now_ts = _mtime_now_ts(wt_dir, RETENTION_HOURS * 10)
    info = _worktree_info(work, branch)

    analyzer = WorktreeAnalyzer(str(work), retention_hours=RETENTION_HOURS, now_ts=now_ts)
    can, code, reason = analyzer.can_remove_worktree(info)
    assert can is False, reason
    assert code == "protected"

    analyzer_force = WorktreeAnalyzer(str(work), force=True, retention_hours=RETENTION_HOURS, now_ts=now_ts)
    can_force, code_force, reason_force = analyzer_force.can_remove_worktree(info)
    assert can_force is False, f"--force не обязан снимать маркер защиты: {reason_force}"
    assert code_force == "protected"

    stats = analyzer.analyze_and_cleanup()
    assert stats["removed"] == 0
    assert [b for b, _m in stats["protected"]] == [branch]
    assert wt_dir.exists(), "дерево с маркером .worktree-keep не должно исчезать с диска"


# ── Сценарий 9: squash-safe признак — переписанный апстрим ─────────────────
# Живой сценарий #1250 (дерево `1211-stale-blocked-prose`): ветка
# force-push'нута (рёбейз) другим каналом уже ПОСЛЕ того, как PR был слит
# squash'ем. Классика (`rev-list --count @{u}..`) отдаёт >0 НАВСЕГДА —
# новый и старый sha относятся к разным линиям истории. Единственный
# признак, переживающий это: содержимое HEAD совпадает с origin/main (не
# sha, а дерево файлов) — условие 2 проходит без оглядки на статус PR,
# условие 3 (PR merged) проверяется отдельно и тоже проходит.

def test_squash_merged_worktree_with_rewritten_upstream_is_removed(tmp_path, monkeypatch):
    work = _setup_repo(tmp_path)
    branch = "agent/9-squash"
    wt_dir = _add_worktree(work, "9-squash", branch)

    (wt_dir / "feature.txt").write_text("feature\n", encoding="utf-8")
    _git(wt_dir, "add", "feature.txt")
    _git(wt_dir, "commit", "-m", "add feature")
    _git(wt_dir, "push", "origin", branch)

    # Squash-слияние в main: НОВЫЙ коммит с тем же содержимым, БЕЗ общего
    # предка с веткой — ровно так выглядит GitHub squash-merge.
    _git(work, "checkout", "main")
    _git(work, "merge", "--squash", branch)
    _git(work, "commit", "-m", "squash: add feature (#1250)")
    _git(work, "push", "origin", "main")

    # Апстрим переписан (force-push амендированного коммита той же веткой).
    # Ветка уже используется worktree'ом wt_dir — "work" не может сделать
    # `git checkout <branch>` (git запрещает второй checkout той же ветки),
    # поэтому коммит собирается напрямую через commit-tree (то же дерево
    # файлов, новое сообщение -> новый sha) и публикуется force-push'ем по
    # refspec без локального переключения веток. `git push` сам обновляет
    # локальный remote-tracking ref — он общий для всех worktree'ов этого
    # репозитория (один .git), поэтому wt_dir увидит новый @{u} немедленно.
    original_tree = _git(wt_dir, "rev-parse", "HEAD^{tree}").stdout.strip()
    original_parent = _git(wt_dir, "rev-parse", "HEAD^").stdout.strip()
    rewritten_sha = _git(
        work, "commit-tree", original_tree, "-p", original_parent,
        "-m", "add feature (rebased elsewhere)",
    ).stdout.strip()
    _git(work, "push", "--force", "origin", f"{rewritten_sha}:refs/heads/{branch}")

    _patch_pr_status(monkeypatch, "merged")

    # Доказываем, что тест реально воспроизводит "классика отдаёт >0" —
    # иначе squash-safe путь не проверяется вовсе, а тест зелёный случайно.
    ahead_output, ahead_rc = worktree_cleanup.run_cmd(
        "git rev-list --count @{u}..", cwd=str(wt_dir)
    )
    assert ahead_rc == 0 and int(ahead_output.strip() or 0) > 0, (
        "постановка теста сломана: апстрим должен выглядеть 'переписанным', "
        f"иначе классический путь уже вернёт ok() и squash-safe fallback не выполнится (rc={ahead_rc}, out={ahead_output!r})"
    )

    now_ts = _mtime_now_ts(wt_dir, RETENTION_HOURS * 10)
    info = _worktree_info(work, branch)

    analyzer = WorktreeAnalyzer(str(work), retention_hours=RETENTION_HOURS, now_ts=now_ts)

    work_result = analyzer.check_unpushed_commits(str(wt_dir), branch)
    assert work_result.status == check_result.STATUS_OK, work_result

    can, code, reason = analyzer.can_remove_worktree(info)
    assert can is True, reason
    assert code == "ok"

    stats = analyzer.analyze_and_cleanup()
    assert stats["removed"] == 1, stats
    assert not wt_dir.exists(), "squash-слитое дерево с переписанным апстримом обязано сняться"


# ── Сценарий 10a: содержимое разошлось, PR доказанно НЕ merged — блок ──────
# Реальная защита: и апстрим, и main показали расхождение, а PR явно
# `closed` (отклонён, не смёржен) — ярус 3 (доверие статусу PR, issue #1250
# раунд 3) честно отказывает, коммиты не доказаны сохранёнными нигде.

def test_content_diverged_and_pr_closed_without_merge_is_refused(tmp_path, monkeypatch):
    _patch_pr_status(monkeypatch, "closed")  # закрыт БЕЗ слияния — не merged
    work = _setup_repo(tmp_path)
    branch = "agent/10a-closed-not-merged"
    wt_dir = _add_worktree(work, "10a-closed-not-merged", branch)

    (wt_dir / "extra.txt").write_text("extra\n", encoding="utf-8")
    _git(wt_dir, "add", "extra.txt")
    _git(wt_dir, "commit", "-m", "local only, PR was closed without merge")
    _delete_remote_branch(work, branch)  # апстрим удалён -> ярус 2 (main), тоже разойдётся

    now_ts = _mtime_now_ts(wt_dir, RETENTION_HOURS * 10)
    info = _worktree_info(work, branch)

    analyzer = WorktreeAnalyzer(str(work), retention_hours=RETENTION_HOURS, now_ts=now_ts)

    work_result = analyzer.check_unpushed_commits(str(wt_dir), branch)
    assert work_result.status == check_result.STATUS_VIOLATION, work_result

    can, code, reason = analyzer.can_remove_worktree(info)
    assert can is False, reason
    assert code == "unpushed"

    stats = analyzer.analyze_and_cleanup()
    assert stats["removed"] == 0
    assert wt_dir.exists(), "PR закрытый без слияния не оправдывает удаление расходящегося содержимого"


# ── Сценарий 10b: ИМЕННО НАЗВАННЫЙ остаточный риск (issue #1250, раунд 3) ──
# Follow-up коммит ПОСЛЕ того, как PR этой же ветки уже смёржен, никогда не
# запушенный никуда — ПРИНЯТЫЙ, задокументированный риск (см. докстринг
# check_unpushed_commits, "ЧЕСТНО НАЗВАННЫЙ ОСТАТОЧНЫЙ РИСК"): в живых
# замерах этого репозитория (#1109 PR #1140, #1027 PR #1030 — оба реально
# MERGED) единственный сигнал, снимающий дерево вообще, — доверие статусу
# PR, когда оба яруса сравнения содержимого разошлись (main непрерывно копит
# чужие изменения, ветки регулярно получают ревью-фиксапы от другого канала
# перед слиянием). Этот тест ФИКСИРУЕТ сегодняшнее поведение как намеренное
# — если кто-то захочет сузить это доверие, тест должен покраснеть и
# заставить осознанно пересмотреть докстринг, а не сломаться тихо.

def test_merged_pr_with_diverged_content_is_trusted_named_risk(tmp_path, monkeypatch):
    _patch_pr_status(monkeypatch, "merged")
    work = _setup_repo(tmp_path)
    branch = "agent/10b-merged-but-diverged"
    wt_dir = _add_worktree(work, "10b-merged-but-diverged", branch)

    (wt_dir / "feature.txt").write_text("v1\n", encoding="utf-8")
    _git(wt_dir, "add", "feature.txt")
    _git(wt_dir, "commit", "-m", "add feature v1")
    _git(wt_dir, "push", "origin", branch)

    _git(work, "checkout", "main")
    _git(work, "merge", "--squash", branch)
    _git(work, "commit", "-m", "squash: add feature v1")
    _git(work, "push", "origin", "main")

    # Follow-up коммит ПОСЛЕ слияния — никогда не запушенный никуда.
    (wt_dir / "feature.txt").write_text("v2 unmerged\n", encoding="utf-8")
    _git(wt_dir, "add", "feature.txt")
    _git(wt_dir, "commit", "-m", "add feature v2 (never pushed anywhere)")

    _delete_remote_branch(work, branch)

    now_ts = _mtime_now_ts(wt_dir, RETENTION_HOURS * 10)
    info = _worktree_info(work, branch)

    analyzer = WorktreeAnalyzer(str(work), retention_hours=RETENTION_HOURS, now_ts=now_ts)

    work_result = analyzer.check_unpushed_commits(str(wt_dir), branch)
    assert work_result.status == check_result.STATUS_OK, (
        f"это ФИКСИРУЕТ принятый риск (issue #1250, раунд 3) — если это красное, "
        f"поведение изменилось и докстринг check_unpushed_commits нужно свериться заново: {work_result}"
    )

    can, code, reason = analyzer.can_remove_worktree(info)
    assert can is True, reason

    stats = analyzer.analyze_and_cleanup()
    assert stats["removed"] == 1, stats


# ── Сценарий 11: --force не снимает проверку сохранности работы ────────────

def test_force_does_not_skip_unproven_work_check(tmp_path, monkeypatch):
    """--force снимает ТОЛЬКО проверку статуса PR как ОТДЕЛЬНЫЙ гейт
    "PR ещё open" (условие 3, issue #1250, п.4). Здесь апстрим удалён,
    локальный коммит реально отличается от main, и статус PR НЕ патчится —
    честный отказ локального bare-репозитория (не GitHub), как в сценарии
    6: ярус 3 внутри `check_unpushed_commits` (условие 2) вызывает
    `get_pr_status` НАПРЯМУЮ и от `self.force` не зависит вовсе — с
    `--force` дерево всё равно остаётся, потому что до отдельного гейта
    "PR open" (условие 3) дело даже не доходит."""
    work = _setup_repo(tmp_path)
    branch = "agent/11-force-no-bypass"
    wt_dir = _add_worktree(work, "11-force-no-bypass", branch)
    (wt_dir / "extra.txt").write_text("extra\n", encoding="utf-8")
    _git(wt_dir, "add", "extra.txt")
    _git(wt_dir, "commit", "-m", "local only, never pushed")
    _delete_remote_branch(work, branch)

    now_ts = _mtime_now_ts(wt_dir, RETENTION_HOURS * 10)
    info = _worktree_info(work, branch)

    analyzer_force = WorktreeAnalyzer(str(work), force=True, retention_hours=RETENTION_HOURS, now_ts=now_ts)
    can, code, reason = analyzer_force.can_remove_worktree(info)
    assert can is False, reason
    assert code == "unknown_work"


# ── Сценарий 12: каталог без .git — не трогается, но попадает в отчёт ──────

def test_orphan_directory_is_reported_not_removed(tmp_path):
    """Каталог под .claude/worktrees без .git (не зарегистрирован `git
    worktree list`) структурно не может быть ни удалён, ни защищён этим
    скриптом (issue #1250, п.5) — но ОБЯЗАН появиться в отчёте, не молчаливо
    игнорироваться."""
    work = _setup_repo(tmp_path)
    orphan_dir = work / ".claude" / "worktrees" / "_scratch_pool"
    orphan_dir.mkdir(parents=True)
    (orphan_dir / "note.txt").write_text("not a worktree\n", encoding="utf-8")

    analyzer = WorktreeAnalyzer(str(work))
    assert analyzer.scan_orphan_directories() == ["_scratch_pool"]

    stats = analyzer.analyze_and_cleanup()
    assert stats["orphan_dirs"] == ["_scratch_pool"]
    assert orphan_dir.exists(), "каталог без .git не должен исчезать"
    assert (orphan_dir / "note.txt").exists(), "содержимое каталога без .git не должно трогаться"


# ── Здоровье скрипта: синтаксис и CLI --dry-run ────────────────────────────

def test_script_compiles():
    result = subprocess.run(
        [sys.executable, "-m", "py_compile", str(_SCRIPT_PATH)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, f"Syntax error in {_SCRIPT_PATH}: {result.stderr}"


def test_dry_run_cli_smoke_on_synthetic_repo(tmp_path):
    """CLI-обвязка (--dry-run) не падает и печатает маркер режима.

    main() берёт repo_root от os.getcwd() (текущего каталога вызова), НЕ от
    расположения самого файла скрипта (__file__) — иначе вызов из
    scripts/git/task-branch внутри песочницы теста task-branch.test.sh
    (`cd "$WORK/x-main" && bash task-branch`) запускал бы уборку по
    НАСТОЯЩЕМУ репозиторию разработчика вместо песочницы (найдено при
    подключении вызова, #891). Здесь это и проверяется: cwd=синтетический
    репозиторий без единого worktree под .claude/worktrees — "Total
    worktrees: 0", реальный dev-репозиторий не затронут вообще."""
    work = _setup_repo(tmp_path)
    result = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH), "--dry-run"],
        cwd=str(work),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert "DRY RUN" in result.stdout
    assert "Total worktrees: 0" in result.stdout
    assert "Removed: 0" in result.stdout


def test_dry_run_cli_smoke_on_real_repo():
    """То же самое, но cwd — настоящий репозиторий (сотни реальных
    worktree'ов, один пакетный gh pr list, не сеть на каждое дерево, класс
    #891) — доказывает, что боевой путь тоже не падает, не только песочница."""
    repo_root = _SCRIPT_PATH.parent.parent.parent
    result = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH), "--dry-run"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    assert "DRY RUN" in result.stdout
    assert "Removed:" in result.stdout


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
