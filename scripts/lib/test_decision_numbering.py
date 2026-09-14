#!/usr/bin/env python3
"""Тесты гвардии номеров ADR/research-документов (#1078).

Прод-форма для git — НАСТОЯЩИЙ временный git-репозиторий (bare "origin" +
рабочий клон), тот же приём, что `scripts/orchestra/test_mechanical_
rebase.py::build_origin` — AGENTS.md, «поведенческий тест находит то, чего
структурный не видит»: гвардия обязана реально фетчить чужую ветку и читать
её дерево `git ls-tree`, не пересказывать ожидаемый результат.

Доказательство мутацией (ручной прогон, дословный вывод — в отчёте PR):
  1. Закомментировать тело `find_number_collisions` (оставить
     `return []`) — `test_two_open_prs_claiming_the_same_number_is_a_collision`
     падает: `assert violations != []` → `AssertionError: assert [] != []`.
  2. Закомментировать вызов `fetch_refs(...)` внутри `collect_sources_from_
     refs` — `test_collect_sources_from_refs_reads_real_git_trees` падает
     на `GitError` (локальный реф `refs/decision-numbering/...` не создан
     фетчем, `ls-tree` не находит его).
  3. Вернуть `parse_numbered_files` к форме `dict[str, str]` (перезапись
     последним, без списка) — `test_intra_source_duplicate_is_detected_on_
     real_git` падает: main после «слияния» двух коллидирующих PR теряет
     один из двух файлов под номером 0017, коллизия не находится.
  4. Вернуть `added_files_under_root` к `--diff-filter=A` (без `R`) —
     `test_rename_of_inherited_file_is_visible_as_added` падает: переномерованный
     файл пропадает из набора источника целиком.
  5. Вернуть `added_files_under_root` к тройной точке (`f"{base}...{ref}"`
     вместо двух отдельных аргументов) — `test_added_files_under_root_works_
     on_a_shallow_pr_clone` падает `GitError: ... no merge base` (находка
     ai-review PR #1082: `actions/checkout@v7` без `fetch-depth: 0` даёт
     shallow-клон, а тройная точка требует merge-base в истории).

Клон одной ветки в тестах — ОБЯЗАТЕЛЬНО через `file://`-URL
(`Path.as_uri()`), не голый путь: git молча ИГНОРИРУЕТ `--depth` при клоне
по локальному пути («warning: --depth is ignored in local clones; use
file:// instead.», проглоченный `capture_output=True`) — так тест до этой
правки гонял ПОЛНУЮ историю и не видел падение на настоящем shallow-клоне
(находка ai-review PR #1082, воспроизведена end-to-end).

Запуск: python -m pytest scripts/lib/test_decision_numbering.py -q
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import subprocess

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "decision_numbering", Path(__file__).with_name("decision_numbering.py"))
dn = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(dn)  # type: ignore[union-attr]


# ── Чистые функции ───────────────────────────────────────────────────────────


def test_parse_numbered_files_matches_files_directly_under_root():
    paths = [
        "docs/decisions/0001-a.md",
        "docs/decisions/0016-b.md",
        "docs/decisions/README.md",  # не пронумерован — игнор
        "docs/research/00-context.md",  # чужой корень — игнор
    ]
    parsed = dn.parse_numbered_files(paths, "docs/decisions", 4)
    assert parsed == {"0001": ["0001-a.md"], "0016": ["0016-b.md"]}


def test_parse_numbered_files_ignores_files_in_subdirectory():
    paths = ["docs/research/data/33-not-directly-under-root.md"]
    parsed = dn.parse_numbered_files(paths, "docs/research", 2)
    assert parsed == {}


def test_parse_numbered_files_keeps_both_names_for_duplicate_number():
    """Находка ai-review PR #1082, блокирующая 1: раньше второе присваивание
    тихо затирало первое (`dict[str, str]`) — список сохраняет оба имени,
    ничего не теряется молча."""
    paths = [
        "docs/decisions/0017-dsh-edge-pr-smoke-local-worker.md",
        "docs/decisions/0017-delayed-branch-deletion-not-delete-on-merge.md",
    ]
    parsed = dn.parse_numbered_files(paths, "docs/decisions", 4)
    assert parsed == {"0017": [
        "0017-dsh-edge-pr-smoke-local-worker.md",
        "0017-delayed-branch-deletion-not-delete-on-merge.md",
    ]}


def test_next_free_number_empty_set_starts_at_one():
    assert dn.next_free_number([], 4) == "0001"


def test_next_free_number_is_max_plus_one():
    assert dn.next_free_number(["0001", "0016", "0017"], 4) == "0018"


def test_find_number_collisions_same_number_same_file_across_sources_is_fine():
    sources = {
        "main": {"0016": ["0016-parallel-work.md"]},
        "PR #900": {"0016": ["0016-parallel-work.md"]},  # PR лишь редактирует существующий
    }
    assert dn.find_number_collisions(sources) == []


def test_two_open_prs_claiming_the_same_number_is_a_collision():
    """Живой класс #1078: два независимых PR берут один и тот же свободный
    номер под РАЗНЫЕ имена файлов."""
    sources = {
        "main": {"0016": ["0016-parallel-work-on-shared-code-boundaries.md"]},
        "PR #1035": {"0017": ["0017-stalled-pr-merge-conflict-triage.md"]},
        "PR #1040": {"0017": ["0017-something-else-entirely.md"]},
    }
    violations = dn.find_number_collisions(sources)
    assert violations != []
    assert violations[0]["number"] == "0017"
    filenames = {occ["filename"] for occ in violations[0]["occurrences"]}
    assert filenames == {
        "0017-stalled-pr-merge-conflict-triage.md",
        "0017-something-else-entirely.md",
    }


def test_two_prs_adding_identical_filename_is_not_flagged_here():
    """Тот же путь в двух PR — add/add-конфликт, который решит git при
    ребейзе; эта гвардия про РАЗНЫЕ имена под одним номером, не про это."""
    sources = {
        "PR #1": {"0020": ["0020-same-name.md"]},
        "PR #2": {"0020": ["0020-same-name.md"]},
    }
    assert dn.find_number_collisions(sources) == []


def test_find_number_collisions_flags_duplicate_within_a_single_source():
    """Находка ai-review PR #1082, блокирующая 1: коллизия ВНУТРИ одного
    источника (main САМ несёт два файла под одним номером — ровно то, что
    получится в main сразу после слияния двух независимо коллидирующих PR)
    обязана находиться тем же механизмом, что и коллизия между источниками."""
    sources = {
        "main": {"0017": [
            "0017-dsh-edge-pr-smoke-local-worker.md",
            "0017-delayed-branch-deletion-not-delete-on-merge.md",
        ]},
    }
    violations = dn.find_number_collisions(sources)
    assert len(violations) == 1
    assert violations[0]["number"] == "0017"
    filenames = {occ["filename"] for occ in violations[0]["occurrences"]}
    assert filenames == {
        "0017-dsh-edge-pr-smoke-local-worker.md",
        "0017-delayed-branch-deletion-not-delete-on-merge.md",
    }


# ── Реальный git: bare "origin" + рабочий клон, как actions/checkout@v7 ─────


def git(*args, cwd):
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, f"git {args} упал в {cwd}: {result.stderr}"
    return result.stdout


def commit_file(repo_dir, rel_path, content, message):
    path = repo_dir / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    git("add", rel_path, cwd=repo_dir)
    git("commit", "-m", message, cwd=repo_dir)


def build_origin_with_number_collision(tmp_path) -> Path:
    """main несёт 0016/0017 (как настоящий репозиторий на 2026-09-12). Две
    ветки-«PR» независимо добавляют номер 0019 под РАЗНЫМИ именами — живой
    класс #1078. Третья ветка добавляет 0020 без коллизии."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(origin), str(seed)], check=True, capture_output=True)
    git("config", "user.email", "test@example.com", cwd=seed)
    git("config", "user.name", "test", cwd=seed)
    commit_file(seed, "docs/decisions/0016-parallel-work.md", "# ADR 0016\n", "main: adr 0016")
    commit_file(seed, "docs/decisions/0017-dsh-edge.md", "# ADR 0017\n", "main: adr 0017")
    git("push", "-u", "origin", "main", cwd=seed)
    base_sha = git("rev-parse", "HEAD", cwd=seed).strip()

    for branch, filename, content in [
        ("agent/1035-pr-conflict-triage", "docs/decisions/0019-stalled-pr-triage.md", "# ADR 0019 (1035)\n"),
        ("agent/1040-other-topic", "docs/decisions/0019-something-else.md", "# ADR 0019 (1040)\n"),
        ("agent/1050-clean", "docs/decisions/0020-clean.md", "# ADR 0020\n"),
    ]:
        git("checkout", "-b", branch, base_sha, cwd=seed)
        commit_file(seed, filename, content, f"{branch}: add {filename}")
        git("push", "-u", "origin", branch, cwd=seed)
        git("checkout", "main", cwd=seed)
    return origin


def clone_workdir(origin: Path, tmp_path: Path, branch: str = "agent/1035-pr-conflict-triage") -> Path:
    """Однобранчевый НАСТОЯЩИЙ shallow-клон (глубина 1) — тот же профиль, что
    `actions/checkout@v7` на PR-прогоне без `fetch-depth: 0`. `file://`-URL —
    ОБЯЗАТЕЛЬНО (находка ai-review PR #1082, блокирующая): git молча
    ИГНОРИРУЕТ `--depth` при клоне по обычному локальному пути (только
    предупреждение в stderr, которое `capture_output=True` проглатывает) —
    клон получается ПОЛНЫМ, и тест на нём не видит поломку `git diff A...B`
    (`no merge base`), которую реальный CI ловит на каждом PR. Assert ниже —
    гвардия этого же факта: если clone однажды перестанет быть shallow
    (другая версия git, другое поведение платформы), тест упадёт здесь
    явно, а не молча продолжит гонять нерепрезентативный полный клон.

    Остальные ссылки (main, чужие PR) ДОЛЖНЫ прийти явным `fetch_refs` — если
    бы тест клонировал все ветки сразу (обычный `git clone`), он не различил
    бы «функция реально фетчит» от «данные и так были локально»."""
    work = tmp_path / "work"
    subprocess.run(
        ["git", "clone", "--branch", branch, "--single-branch", "--depth", "1",
         origin.as_uri(), str(work)],
        check=True, capture_output=True,
    )
    git("config", "user.email", "test@example.com", cwd=work)
    git("config", "user.name", "test", cwd=work)
    assert (work / ".git" / "shallow").exists(), (
        "клон не получился shallow — file://+--depth не сработал на этой "
        "платформе/версии git, тест перестал воспроизводить профиль CI"
    )
    return work


def test_collect_sources_from_refs_reads_real_git_trees(tmp_path):
    origin = build_origin_with_number_collision(tmp_path)
    work = clone_workdir(origin, tmp_path)

    refs = {
        "main": "main",
        "PR #1035": "agent/1035-pr-conflict-triage",
        "PR #1040": "agent/1040-other-topic",
        "PR #1050": "agent/1050-clean",
    }
    sources = dn.collect_sources_from_refs(refs, "docs/decisions", 4, cwd=work)

    assert sources["main"] == {"0016": ["0016-parallel-work.md"], "0017": ["0017-dsh-edge.md"]}
    assert sources["PR #1035"]["0019"] == ["0019-stalled-pr-triage.md"]
    assert sources["PR #1040"]["0019"] == ["0019-something-else.md"]
    assert sources["PR #1050"]["0020"] == ["0020-clean.md"]


def test_added_files_under_root_works_on_a_shallow_pr_clone(tmp_path):
    """Находка ai-review PR #1082, единственная блокирующая: `actions/
    checkout@v7` без `fetch-depth: 0` (реальный профиль job'а `test` в
    repo-ci.yml) даёт shallow-клон PR-ветки — `fetch_refs` приносит main с
    полной историей отдельно, но общего предка между shallow PR-веткой и
    main взять неоткуда. Тройная точка (`git diff A...B`) требует его и
    падает `no merge base`, exit 128 → `GitError` — гвардия красила бы
    `test` НА КАЖДОМ PR репозитория, не только у виновника. Прямая проверка
    именно этого пути, отдельно от общего сценария выше."""
    origin = build_origin_with_number_collision(tmp_path)
    work = clone_workdir(origin, tmp_path, branch="agent/1035-pr-conflict-triage")

    dn.fetch_refs({"main": "main", "PR #1035": "agent/1035-pr-conflict-triage"}, cwd=work)

    added = dn.added_files_under_root("main", "PR #1035", "docs/decisions", cwd=work)

    assert added == ["docs/decisions/0019-stalled-pr-triage.md"]


def test_end_to_end_detects_live_1078_collision_on_real_git(tmp_path):
    origin = build_origin_with_number_collision(tmp_path)
    work = clone_workdir(origin, tmp_path)

    refs = {
        "main": "main",
        "PR #1035": "agent/1035-pr-conflict-triage",
        "PR #1040": "agent/1040-other-topic",
        "PR #1050": "agent/1050-clean",
    }
    sources = dn.collect_sources_from_refs(refs, "docs/decisions", 4, cwd=work)
    violations = dn.find_number_collisions(sources)

    assert len(violations) == 1
    assert violations[0]["number"] == "0019"
    involved = {src for occ in violations[0]["occurrences"] for src in occ["sources"]}
    assert involved == {"PR #1035", "PR #1040"}
    # 0020 (PR #1050) чист — единственный источник, коллизии нет.
    assert not any(v["number"] == "0020" for v in violations)
    # Живой фикс (2026-09-13, найден живым прогоном на mytab0r/edge-harness):
    # PR несёт УНАСЛЕДОВАННЫЕ (не им добавленные) 0016/0017 main — они не
    # должны попасть в его собственный набор источников вовсе, иначе PR,
    # просто содержащий уже смердженный ADR, ложно считался бы «участником»
    # любой коллизии вокруг этого номера.
    assert "0016" not in sources["PR #1035"]
    assert "0017" not in sources["PR #1035"]


def test_collect_sources_from_refs_raises_git_error_on_unknown_branch(tmp_path):
    origin = build_origin_with_number_collision(tmp_path)
    work = clone_workdir(origin, tmp_path)

    with pytest.raises(dn.GitError):
        dn.collect_sources_from_refs({"main": "does-not-exist"}, "docs/decisions", 4, cwd=work)


def build_origin_with_intra_main_duplicate(tmp_path) -> Path:
    """`main` САМ несёт два файла под одним номером — ровно состояние ПОСЛЕ
    того, как два независимо коллидирующих PR оба слились (пути разные, git
    не видит конфликта, слияние проходит тихо). Живой класс #1078 (третий
    случай, PR #944 vs main), симулированный напрямую в main для теста
    intra-source коллизии (ai-review PR #1082, блокирующая 1)."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(origin), str(seed)], check=True, capture_output=True)
    git("config", "user.email", "test@example.com", cwd=seed)
    git("config", "user.name", "test", cwd=seed)
    commit_file(
        seed, "docs/decisions/0017-dsh-edge-pr-smoke-local-worker.md",
        "# ADR 0017 (оригинал)\n", "main: adr 0017 оригинал",
    )
    commit_file(
        seed, "docs/decisions/0017-delayed-branch-deletion-not-delete-on-merge.md",
        "# ADR 0017 (чужой PR, слился без конфликта)\n", "main: слияние коллидирующего PR",
    )
    git("push", "-u", "origin", "main", cwd=seed)
    return origin


def test_intra_source_duplicate_is_detected_on_real_git(tmp_path):
    origin = build_origin_with_intra_main_duplicate(tmp_path)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", str(origin), str(work)], check=True, capture_output=True)
    git("config", "user.email", "test@example.com", cwd=work)
    git("config", "user.name", "test", cwd=work)

    sources = dn.collect_sources_from_refs({"main": "main"}, "docs/decisions", 4, cwd=work)
    violations = dn.find_number_collisions(sources)

    assert len(violations) == 1
    assert violations[0]["number"] == "0017"
    filenames = {occ["filename"] for occ in violations[0]["occurrences"]}
    assert filenames == {
        "0017-dsh-edge-pr-smoke-local-worker.md",
        "0017-delayed-branch-deletion-not-delete-on-merge.md",
    }


def build_origin_with_renamed_file(tmp_path) -> Path:
    """main несёт 0016-*.md; PR-ветка ПЕРЕНОМЕРОВЫВАЕТ (git mv) унаследованный
    файл в 0022-*.md, не создавая новый с нуля. Против `main` это `git diff
    --name-status` статус `R100 старый\tновый`, не `A новый`."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(origin), str(seed)], check=True, capture_output=True)
    git("config", "user.email", "test@example.com", cwd=seed)
    git("config", "user.name", "test", cwd=seed)
    commit_file(seed, "docs/decisions/0016-old-name.md", "# ADR 0016, довольно длинный текст для схожести\n" * 5, "main: adr 0016")
    git("push", "-u", "origin", "main", cwd=seed)

    git("checkout", "-b", "agent/1060-renumber", cwd=seed)
    git("mv", "docs/decisions/0016-old-name.md", "docs/decisions/0022-renumbered.md", cwd=seed)
    git("commit", "-m", "agent/1060-renumber: renumber 0016 -> 0022", cwd=seed)
    git("push", "-u", "origin", "agent/1060-renumber", cwd=seed)
    return origin


def test_rename_of_inherited_file_is_visible_as_added(tmp_path):
    origin = build_origin_with_renamed_file(tmp_path)
    work = clone_workdir(origin, tmp_path, branch="agent/1060-renumber")

    sources = dn.collect_sources_from_refs(
        {"main": "main", "PR #1060": "agent/1060-renumber"}, "docs/decisions", 4, cwd=work,
    )

    assert sources["PR #1060"] == {"0022": ["0022-renumbered.md"]}
    # 0016 не должен всплыть у PR как «его собственный источник» — это main.
    assert "0016" not in sources["PR #1060"]


# ── collect_sources: тонкая обвязка gh (метаданные, не git) ─────────────────


def test_collect_sources_translates_open_pulls_into_refs(monkeypatch):
    calls = []

    def fake_gh(*args):
        assert args == ("repos/owner/repo/pulls?state=open&per_page=100&page=1",)
        # Короткая страница (2 < 100) — list_pages останавливается после неё.
        return [
            {"number": 1035, "head": {"ref": "agent/1035-pr-conflict-triage"}},
            {"number": 1040, "head": {"ref": "agent/1040-other-topic"}},
        ]

    def fake_collect_from_refs(refs, root, width, cwd=None):
        calls.append(refs)
        return {}

    monkeypatch.setattr(dn, "gh", fake_gh)
    monkeypatch.setattr(dn, "collect_sources_from_refs", fake_collect_from_refs)

    dn.collect_sources("owner/repo", "docs/decisions", 4)

    assert calls == [{
        "main": "main",
        "PR #1035": "agent/1035-pr-conflict-triage",
        "PR #1040": "agent/1040-other-topic",
    }]


# ── cmd_check: не красить чужой PR чужой коллизией ──────────────────────────
#
# Живой случай (2026-09-13, найден живым прогоном этого модуля на
# mytab0r/edge-harness): PR #944 независимо занял номер 0017, уже слитый в
# main другим ADR — 20+ ПОСТОРОННИХ открытых PR совпадают с main. Без
# сужения по self_source_name это покрасило бы CI КАЖДОГО из них (AGENTS.md,
# «тормоз без газа не принимается»).


def test_cmd_check_only_flags_violation_involving_current_branch(monkeypatch):
    refs = {"main": "main", "PR #944": "agent/940-artifact-retention", "PR #1035": "agent/1035-pr-conflict-triage"}
    sources = {
        "main": {"0017": ["0017-dsh-edge.md"]},
        "PR #944": {"0017": ["0017-delayed-branch-deletion.md"]},  # коллизия с main
        "PR #1035": {"0019": ["0019-triage.md"]},  # ни с кем не конфликтует
    }
    monkeypatch.setattr(dn, "build_refs", lambda repo: refs)
    monkeypatch.setattr(dn, "collect_sources_from_refs", lambda r, root, width, cwd=None: sources)

    # Прогон CI самого PR #1035 (не участвует в коллизии 0017) — обязан быть чист.
    monkeypatch.setenv("GITHUB_HEAD_REF", "agent/1035-pr-conflict-triage")
    monkeypatch.delenv("GITHUB_REF_NAME", raising=False)
    lines, self_name = dn.cmd_check("owner/repo", ["docs/decisions"])
    assert lines == []
    assert self_name == "PR #1035"

    # Прогон CI виновника (PR #944) — обязан покраснеть.
    monkeypatch.setenv("GITHUB_HEAD_REF", "agent/940-artifact-retention")
    lines, self_name = dn.cmd_check("owner/repo", ["docs/decisions"])
    assert len(lines) == 1
    assert "0017" in lines[0]
    assert self_name == "PR #944"


def test_cmd_check_reports_everything_when_branch_unknown(monkeypatch):
    refs = {"main": "main", "PR #944": "agent/940-artifact-retention"}
    sources = {
        "main": {"0017": ["0017-dsh-edge.md"]},
        "PR #944": {"0017": ["0017-delayed-branch-deletion.md"]},
    }
    monkeypatch.setattr(dn, "build_refs", lambda repo: refs)
    monkeypatch.setattr(dn, "collect_sources_from_refs", lambda r, root, width, cwd=None: sources)
    monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
    monkeypatch.delenv("GITHUB_REF_NAME", raising=False)

    lines, self_name = dn.cmd_check("owner/repo", ["docs/decisions"])

    assert len(lines) == 1  # честный дефолт «не знаю → покажи всё», не «не знаю → молчи»


# ── cmd_check: main виновен только за коллизию ВНУТРИ main (issue #1200) ────
#
# Живой случай (2026-09-13/14, найден пост-мерж прогоном repo-ci.yml на
# mytab0r/edge-harness, 12+ красных подряд): push/workflow_dispatch на main
# резолвит self_name в буквальное "main", а main легитимно держит номер,
# который НЕЗАВИСИМО занял сторонний, ещё не смёрженный PR (#944 — 0017,
# #667 — 0018) — старое условие `self_name in involved` считало main
# виновным просто потому, что main тоже входит в involved этой коллизии.


def test_cmd_check_does_not_blame_main_for_a_foreign_open_pr_collision(monkeypatch):
    refs = {"main": "main", "PR #944": "agent/940-artifact-retention"}
    sources = {
        "main": {"0017": ["0017-dsh-edge-pr-smoke-local-worker.md"]},
        "PR #944": {"0017": ["0017-delayed-branch-deletion-not-delete-on-merge.md"]},
    }
    monkeypatch.setattr(dn, "build_refs", lambda repo: refs)
    monkeypatch.setattr(dn, "collect_sources_from_refs", lambda r, root, width, cwd=None: sources)

    # Прогон push/workflow_dispatch на main: GITHUB_REF_NAME=main, HEAD_REF нет.
    monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
    monkeypatch.setenv("GITHUB_REF_NAME", "main")

    lines, self_name = dn.cmd_check("owner/repo", ["docs/decisions"])

    assert self_name == "main"
    assert lines == []  # main легитимен, долг — на PR #944, не на main


def test_cmd_check_blames_main_for_an_intra_main_duplicate(monkeypatch):
    refs = {"main": "main"}
    sources = {
        "main": {"0017": [
            "0017-dsh-edge-pr-smoke-local-worker.md",
            "0017-delayed-branch-deletion-not-delete-on-merge.md",
        ]},
    }
    monkeypatch.setattr(dn, "build_refs", lambda repo: refs)
    monkeypatch.setattr(dn, "collect_sources_from_refs", lambda r, root, width, cwd=None: sources)
    monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
    monkeypatch.setenv("GITHUB_REF_NAME", "main")

    lines, self_name = dn.cmd_check("owner/repo", ["docs/decisions"])

    assert self_name == "main"
    assert len(lines) == 1  # настоящий дубль ВНУТРИ main — main обязан покраснеть
    assert "0017" in lines[0]


def build_origin_with_main_vs_foreign_pr_collision(tmp_path) -> Path:
    """main держит 0017 легитимно; ОДИН сторонний PR независимо занял тот же
    номер другим именем — ровно живой случай #944 (issue #1200), не синтетика
    по аналогии: дословные имена файлов и номер из живой находки."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(origin), str(seed)], check=True, capture_output=True)
    git("config", "user.email", "test@example.com", cwd=seed)
    git("config", "user.name", "test", cwd=seed)
    commit_file(
        seed, "docs/decisions/0017-dsh-edge-pr-smoke-local-worker.md",
        "# ADR 0017\n", "main: adr 0017",
    )
    git("push", "-u", "origin", "main", cwd=seed)
    base_sha = git("rev-parse", "HEAD", cwd=seed).strip()

    git("checkout", "-b", "agent/940-artifact-retention", base_sha, cwd=seed)
    commit_file(
        seed, "docs/decisions/0017-delayed-branch-deletion-not-delete-on-merge.md",
        "# ADR 0017 (чужой)\n", "agent/940-artifact-retention: add 0017",
    )
    git("push", "-u", "origin", "agent/940-artifact-retention", cwd=seed)
    return origin


def test_end_to_end_main_push_is_not_blamed_for_foreign_pr_on_real_git(tmp_path, monkeypatch):
    """Поведенческое доказательство issue #1200 на настоящем git-дереве, не
    только на монки-патче сверху: main держит легитимный файл, сторонний PR
    независимо занял тот же номер — прогон main (self_name == "main") обязан
    остаться чист, прогон самого PR — обязан покраснеть."""
    origin = build_origin_with_main_vs_foreign_pr_collision(tmp_path)
    work = clone_workdir(origin, tmp_path, branch="agent/940-artifact-retention")

    refs = {"main": "main", "PR #944": "agent/940-artifact-retention"}
    monkeypatch.setattr(dn, "build_refs", lambda repo: refs)

    monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
    monkeypatch.setenv("GITHUB_REF_NAME", "main")
    lines, self_name = dn.cmd_check("owner/repo", ["docs/decisions"], cwd=work)
    assert self_name == "main"
    assert lines == []

    monkeypatch.delenv("GITHUB_REF_NAME", raising=False)
    monkeypatch.setenv("GITHUB_HEAD_REF", "agent/940-artifact-retention")
    lines, self_name = dn.cmd_check("owner/repo", ["docs/decisions"], cwd=work)
    assert self_name == "PR #944"
    assert len(lines) == 1
    assert "0017" in lines[0]


# ── check_decision_doc_number_collisions: обвязка для repo_invariants.py ────
#
# НЕ подключена в repo_invariants.py этим PR (см. докстринг функции —
# PR #1076 параллельно правит тот файл, класс ADR 0016). Тест проверяет
# только саму обвязку — форму возврата, готовую для будущего подключения.


def test_check_decision_doc_number_collisions_adds_root_to_each_violation(monkeypatch):
    def fake_collect_sources(repo, root, width, cwd=None):
        if root == "docs/decisions":
            return {
                "main": {"0017": ["0017-a.md"]},
                "PR #944": {"0017": ["0017-b.md"]},
            }
        return {}

    monkeypatch.setattr(dn, "collect_sources", fake_collect_sources)

    result = dn.check_decision_doc_number_collisions("owner/repo")

    assert result.status == dn.check_result.STATUS_VIOLATION
    assert len(result.violations) == 1
    assert result.violations[0]["root"] == "docs/decisions"
    assert result.violations[0]["number"] == "0017"


def test_check_decision_doc_number_collisions_ok_when_no_collisions(monkeypatch):
    monkeypatch.setattr(dn, "collect_sources", lambda repo, root, width, cwd=None: {})

    result = dn.check_decision_doc_number_collisions("owner/repo")

    assert result == dn.check_result.ok()


def test_check_decision_doc_number_collisions_unknown_on_transport_failure(monkeypatch):
    def fake_collect_sources(repo, root, width, cwd=None):
        raise dn.GhError("HTTP 502")

    monkeypatch.setattr(dn, "collect_sources", fake_collect_sources)

    result = dn.check_decision_doc_number_collisions("owner/repo")

    assert result.status == dn.check_result.STATUS_UNKNOWN
    assert result.reason  # честная причина, не пустая строка
    assert result.violations == []
