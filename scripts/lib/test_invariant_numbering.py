#!/usr/bin/env python3
"""Тесты гвардии номеров инвариантов `repo_invariants.py` (#904).

Прод-форма для git — НАСТОЯЩИЙ временный git-репозиторий (bare "origin" +
рабочий клон), тот же приём, что `scripts/lib/test_decision_numbering.py`
(#1078) — AGENTS.md, «поведенческий тест находит то, чего структурный не
видит»: гвардия обязана реально фетчить чужую ветку и читать её дерево
`git show`/`git diff`, не пересказывать ожидаемый результат.

`test_end_to_end_detects_live_904_collision_on_real_git` — НЕ синтетика по
аналогии (issue #1194): текст записей 17/18/19 в фикстуре — ДОСЛОВНАЯ копия
реестра из реальных веток `origin/main`, `origin/agent/925-ci-run-on-main`
(PR #1061, issue #925) и `origin/agent/1121-soft-failure-digest` (PR #1136,
issue #1121) на 2026-09-14 (снята командой `git show <ref>:scripts/
orchestra/repo_invariants.py`, приведена дословно, не по памяти) — оба PR
независимо добавили инвариант 19 разными функциями
(`check_ci_failure_closed_but_main_red` и `check_continue_on_error_readers`).
Это и есть живая коллизия, ради которой заведена задача #904.

Доказательство мутацией (ручной прогон, дословный вывод — в отчёте PR):
  1. Заменить тело `find_number_collisions` (импортировано из
     `decision_numbering.py`) эквивалентом `return []` —
     `test_two_open_prs_claiming_the_same_invariant_number_is_a_collision`
     и `test_end_to_end_detects_live_904_collision_on_real_git` падают:
     `assert violations != []` → `AssertionError`.
  2. Вернуть `added_registry_entries` к чтению ПОЛНОГО реестра ветки (не
     только добавленного ею) — `test_added_registry_entries_excludes_
     inherited_main_entries` падает: унаследованные 17/18 всплывают в
     наборе источника PR, которому не принадлежат.
  3. Вернуть `added_registry_entries` к тройной точке (`f"{base}...{ref}"`)
     — `test_collect_sources_from_refs_works_on_a_shallow_pr_clone` падает
     `GitError: ... no merge base` (тот же класс, что нашла ai-review
     PR #1082 у decision_numbering.py: shallow-клон без общего предка).

Клон одной ветки — ОБЯЗАТЕЛЬНО через `file://`-URL (`Path.as_uri()`), не
голый путь: git молча игнорирует `--depth` при клоне по локальному пути.

Запуск: python -m pytest scripts/lib/test_invariant_numbering.py -q
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import re
import subprocess

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "invariant_numbering", Path(__file__).with_name("invariant_numbering.py"))
inv = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(inv)  # type: ignore[union-attr]


# ── Чистые функции ───────────────────────────────────────────────────────────


def test_parse_registry_entries_matches_the_real_registry_line_form():
    text = (
        '  1. check_reopened_after_merge — открытая задача.\n'
        '  2. (retired) check_free_task_count_mismatch сравнивал.\n'
        '  Не запись реестра — игнор.\n'
    )
    parsed = inv.parse_registry_entries(text)
    assert parsed == {
        "1": ["check_reopened_after_merge"],
        "2": ["check_free_task_count_mismatch"],
    }


def test_parse_registry_entries_keeps_both_names_for_duplicate_number():
    """Тот же класс, что `decision_numbering.parse_numbered_files`: main сам
    может нести два инварианта под одним номером после тихого слияния двух
    коллидирующих PR — список сохраняет оба имени, ничего не теряется."""
    text = (
        "  19. check_ci_failure_closed_but_main_red (issue #925) — ...\n"
        "  19. check_continue_on_error_readers (#1121) — ...\n"
    )
    parsed = inv.parse_registry_entries(text)
    assert parsed == {"19": ["check_ci_failure_closed_but_main_red", "check_continue_on_error_readers"]}


def test_parse_registry_entries_ignores_non_registry_numbered_lines():
    """Строки вида «инвариант N (...)» (эскалационные блоки, комментарии) не
    матчат формат реестра «N. check_...» — регэксп не должен их подхватывать."""
    text = (
        '            "🚨 edge-harness: инвариант 12 (...) — "\n'
        "  findings[12] = v12\n"
    )
    assert inv.parse_registry_entries(text) == {}


def test_parse_registry_entries_tolerates_markdown_wrapping_around_the_function_name():
    """Находка ai-review PR #1201: правдоподобная форма будущей записи — имя
    функции обёрнуто бэктиками/жирным (авторы реестра уже перенумеровывали
    его руками под давлением мержа, тот же риск дрейфа формата). Регэксп
    обязан видеть запись, а не молчать (класс #891/#893)."""
    text = (
        "  19. `check_something_new` (#1) — обёрнуто бэктиками.\n"
        "  20. **check_bold_wrap** (#2) — обёрнуто жирным.\n"
    )
    parsed = inv.parse_registry_entries(text)
    assert parsed == {
        "19": ["check_something_new"],
        "20": ["check_bold_wrap"],
    }


def test_parse_registry_entries_pins_the_live_repo_invariants_registry():
    """Пин к ЖИВОМУ scripts/orchestra/repo_invariants.py (находка ai-review
    PR #1201): переименование функции реестра, дрейф формата строки реестра
    ИЛИ появление незарегистрированной check_-функции верхнего уровня красит
    этот тест, а не проходит молча (класс #891/#893, «гвардия слепнет молча
    при дрейфе формата») — ровно тот прогон, который #904 обязан ловить.
    `check_ai_failed_budget_exhausted` — под-проверка ВНУТРИ инварианта 3
    (`check_stuck_review_gate`), не отдельный член реестра. Запись 2
    отмечена (retired) — `check_free_task_count_mismatch` в реестре
    остался, но def-а в файле больше нет."""
    repo_root = Path(__file__).resolve().parents[2]
    live_path = repo_root / inv.TARGET_PATH
    text = live_path.read_text(encoding="utf-8")
    parsed = inv.parse_registry_entries(text)

    # Не range(1, N): #1253 занял 22 (арбитр `invariant_numbering.py next` —
    # 19/20/21 заняты сторонними открытыми PR #1061/#1136/#1247 на
    # 2026-09-14), поэтому реестр main+этой ветки — 1..18 подряд плюс 22,
    # с разрывом до слияния тех PR. Разрыв — ожидаемое следствие арбитража
    # по открытым PR, не дрейф формата.
    assert set(parsed) == {str(n) for n in range(1, 19)} | {"22"}, (
        "диапазон номеров реестра изменился (см. docstring "
        "scripts/orchestra/repo_invariants.py) — обнови этот пин, ИЛИ "
        "REGISTRY_ENTRY_RE перестал видеть живую запись"
    )

    registry_functions = {name for names in parsed.values() for name in names}
    defined_functions = set(re.findall(r"^def (check_\w+)\(", text, flags=re.MULTILINE))
    NOT_A_REGISTRY_MEMBER = {"check_ai_failed_budget_exhausted"}
    RETIRED_NOT_A_DEF = {"check_free_task_count_mismatch"}

    assert registry_functions - defined_functions == RETIRED_NOT_A_DEF, (
        "реестр ссылается на функцию, которой нет в файле (кроме известной "
        "retired-записи) — либо переименовали def, не поправив реестр, либо "
        "парсер прочитал не то"
    )
    assert defined_functions - registry_functions == NOT_A_REGISTRY_MEMBER, (
        "в файле появилась НЕзарегистрированная check_-функция верхнего "
        "уровня — либо забыли добавить её в реестр, либо это новый "
        "не-член реестра (тогда назови его явно в NOT_A_REGISTRY_MEMBER "
        "этого теста)"
    )


def test_find_number_collisions_reused_from_decision_numbering_same_number_same_function_is_fine():
    sources = {
        "main": {"18": ["check_pipeline_status_marker_impersonation"]},
        "PR #900": {"18": ["check_pipeline_status_marker_impersonation"]},  # PR лишь редактирует существующий
    }
    assert inv.dn.find_number_collisions(sources) == []


def test_two_open_prs_claiming_the_same_invariant_number_is_a_collision():
    """Живой класс #904, найден этим PR: два независимых открытых PR берут
    один и тот же свободный номер инварианта под РАЗНЫЕ функции — ровно
    #1061 (check_ci_failure_closed_but_main_red) против #1136
    (check_continue_on_error_readers), оба претендуют на 19."""
    sources = {
        "main": {"18": ["check_pipeline_status_marker_impersonation"]},
        "PR #1061": {"19": ["check_ci_failure_closed_but_main_red"]},
        "PR #1136": {"19": ["check_continue_on_error_readers"]},
    }
    violations = inv.dn.find_number_collisions(sources)
    assert violations != []
    assert violations[0]["number"] == "19"
    names = {occ["filename"] for occ in violations[0]["occurrences"]}
    assert names == {"check_ci_failure_closed_but_main_red", "check_continue_on_error_readers"}


def test_cmd_next_returns_max_plus_one_across_all_sources():
    assert inv.dn.next_free_number(["1", "17", "18"], 1) == "19"
    assert inv.dn.next_free_number([], 1) == "1"


def test_format_violation_names_both_functions_and_sources():
    violation = {
        "number": "19",
        "occurrences": [
            {"filename": "check_ci_failure_closed_but_main_red", "sources": ["PR #1061"]},
            {"filename": "check_continue_on_error_readers", "sources": ["PR #1136"]},
        ],
    }
    line = inv.format_violation(violation)
    assert "19" in line
    assert "check_ci_failure_closed_but_main_red" in line
    assert "check_continue_on_error_readers" in line
    assert inv.TARGET_PATH in line


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


# Дословные фрагменты реестра, снятые командой `git show <ref>:scripts/
# orchestra/repo_invariants.py` на 2026-09-14 (issue #1194 — по факту, не по
# аналогии). Урезаны до первых строк каждой записи (регэксп матчит первую
# строку записи; остальной абзац для теста не нужен) — САМИ строки не
# перефразированы.
_ENTRY_17_MAIN = (
    "  17. check_frontend_deploy_stale (issue #1041, живой случай 2026-09-12/13):\n"
    "      живая морда dsh-edge отстаёт от main.\n"
)
_ENTRY_18_MAIN = (
    "  18. check_pipeline_status_marker_impersonation (#1101, найдено при доводке\n"
    "      #1074/PR #1077, живой инцидент watchdog-issue #120, 2026-09-12/13):\n"
    "      комментарий #120 без требуемой атрибуции.\n"
)
_ENTRY_19_PR_925 = (
    "  19. check_ci_failure_closed_but_main_red (issue #925) — ci-failure задача\n"
    "      закрыта за окно CI_FAILURE_RESOLVED_WINDOW_HOURS, а последний\n"
    "      ПОКАЗАТЕЛЬНЫЙ прогон её workflow на main красный.\n"
)
_ENTRY_19_PR_1121 = (
    "  19. check_continue_on_error_readers (#1121) — инвариант «прогон зелёный,\n"
    "      а шаг красный» (номер 17 занят параллельным #1076/check_frontend_\n"
    "      deploy_stale, 18 — #1101/check_pipeline_status_marker_impersonation).\n"
)


def _module_stub(*entries: str) -> str:
    triple_quote = '"' * 3
    return (
        "#!/usr/bin/env python3\n"
        + triple_quote + "Инварианты состояния репозитория (#244).\n"
        + "\n"
        + "".join(entries)
        + triple_quote + "\n"
    )


def build_origin_with_live_904_collision(tmp_path) -> Path:
    """main несёт реальные (дословные) записи 17/18, две ветки-«PR» —
    точные копии реальных веток origin/agent/925-ci-run-on-main (PR #1061) и
    origin/agent/1121-soft-failure-digest (PR #1136) на 2026-09-14 — НЕЗАВИСИМО
    добавляют запись 19 под РАЗНЫМи именами функций —
    живая коллизия #904. Третья ветка добавляет 20 без коллизии."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(origin), str(seed)], check=True, capture_output=True)
    git("config", "user.email", "test@example.com", cwd=seed)
    git("config", "user.name", "test", cwd=seed)
    commit_file(
        seed, inv.TARGET_PATH, _module_stub(_ENTRY_17_MAIN, _ENTRY_18_MAIN),
        "main: инварианты 17/18",
    )
    git("push", "-u", "origin", "main", cwd=seed)
    base_sha = git("rev-parse", "HEAD", cwd=seed).strip()

    for branch, entry, label in [
        ("agent/925-ci-run-on-main", _ENTRY_19_PR_925, "PR #1061 (issue #925): инвариант 19"),
        ("agent/1121-soft-failure-digest", _ENTRY_19_PR_1121, "PR #1136 (issue #1121): инвариант 19"),
    ]:
        git("checkout", "-b", branch, base_sha, cwd=seed)
        commit_file(
            seed, inv.TARGET_PATH,
            _module_stub(_ENTRY_17_MAIN, _ENTRY_18_MAIN, entry), label,
        )
        git("push", "-u", "origin", branch, cwd=seed)
        git("checkout", "main", cwd=seed)

    git("checkout", "-b", "agent/1050-clean", base_sha, cwd=seed)
    commit_file(
        seed, inv.TARGET_PATH,
        _module_stub(_ENTRY_17_MAIN, _ENTRY_18_MAIN, "  20. check_clean_addition (#1) — без коллизии.\n"),
        "agent/1050-clean: инвариант 20",
    )
    git("push", "-u", "origin", "agent/1050-clean", cwd=seed)
    return origin


def clone_workdir(origin: Path, tmp_path: Path, branch: str = "agent/925-ci-run-on-main") -> Path:
    """Однобранчевый НАСТОЯЩИЙ shallow-клон (глубина 1) — тот же профиль,
    что `actions/checkout@v7` на PR-прогоне без `fetch-depth: 0`. `file://`-URL —
    ОБЯЗАТЕЛЬНО (та же находка, что в test_decision_numbering.py): git молча игнорирует
    `--depth` при клоне по локальному пути."""
    work = tmp_path / "work"
    subprocess.run(
        ["git", "clone", "--branch", branch, "--single-branch", "--depth", "1",
         origin.as_uri(), str(work)],
        check=True, capture_output=True,
    )
    git("config", "user.email", "test@example.com", cwd=work)
    git("config", "user.name", "test", cwd=work)
    assert (work / ".git" / "shallow").exists(), (
        "клон не получился shallow — тест перестал воспроизводить профиль CI"
    )
    return work


def test_collect_sources_from_refs_reads_real_git_trees(tmp_path):
    origin = build_origin_with_live_904_collision(tmp_path)
    work = clone_workdir(origin, tmp_path)

    refs = {
        "main": "main",
        "PR #1061": "agent/925-ci-run-on-main",
        "PR #1136": "agent/1121-soft-failure-digest",
        "PR #1050": "agent/1050-clean",
    }
    sources = inv.collect_sources_from_refs(refs, inv.TARGET_PATH, cwd=work)

    assert sources["main"] == {
        "17": ["check_frontend_deploy_stale"],
        "18": ["check_pipeline_status_marker_impersonation"],
    }
    assert sources["PR #1061"] == {"19": ["check_ci_failure_closed_but_main_red"]}
    assert sources["PR #1136"] == {"19": ["check_continue_on_error_readers"]}
    assert sources["PR #1050"] == {"20": ["check_clean_addition"]}


def test_added_registry_entries_excludes_inherited_main_entries(tmp_path):
    """PR несёт УНАСЛЕДОВАННЫЕ (не им добавленные) 17/18 от main —
    они не должны попасть в его собственный набор источников вовсе, иначе
    любой посторонний PR стал бы мнимым участником любой коллизии вокруг них."""
    origin = build_origin_with_live_904_collision(tmp_path)
    work = clone_workdir(origin, tmp_path, branch="agent/925-ci-run-on-main")
    inv.dn.fetch_refs({"main": "main", "PR #1061": "agent/925-ci-run-on-main"}, cwd=work)

    added = inv.added_registry_entries("main", "PR #1061", inv.TARGET_PATH, cwd=work)

    assert added == {"19": ["check_ci_failure_closed_but_main_red"]}
    assert "17" not in added
    assert "18" not in added


def test_collect_sources_from_refs_works_on_a_shallow_pr_clone(tmp_path):
    """Тот же класс, что у decision_numbering.py (ai-review PR #1082): без двух
    отдельных рефов (`git diff A B`, не `A...B`) тройная точка требует
    merge-base и падает на shallow-клоне без общего предка."""
    origin = build_origin_with_live_904_collision(tmp_path)
    work = clone_workdir(origin, tmp_path, branch="agent/925-ci-run-on-main")

    refs = {"main": "main", "PR #1061": "agent/925-ci-run-on-main"}
    sources = inv.collect_sources_from_refs(refs, inv.TARGET_PATH, cwd=work)

    assert sources["PR #1061"] == {"19": ["check_ci_failure_closed_but_main_red"]}


def test_end_to_end_detects_live_904_collision_on_real_git(tmp_path):
    origin = build_origin_with_live_904_collision(tmp_path)
    work = clone_workdir(origin, tmp_path)

    refs = {
        "main": "main",
        "PR #1061": "agent/925-ci-run-on-main",
        "PR #1136": "agent/1121-soft-failure-digest",
        "PR #1050": "agent/1050-clean",
    }
    sources = inv.collect_sources_from_refs(refs, inv.TARGET_PATH, cwd=work)
    violations = inv.dn.find_number_collisions(sources)

    assert len(violations) == 1
    assert violations[0]["number"] == "19"
    involved = {src for occ in violations[0]["occurrences"] for src in occ["sources"]}
    assert involved == {"PR #1061", "PR #1136"}
    # 20 (PR #1050) чист — единственный источник, коллизии нет.
    assert not any(v["number"] == "20" for v in violations)


def build_origin_with_unreadable_main_registry(tmp_path) -> Path:
    """main несёт непустой файл по `TARGET_PATH`, но ни одна строка не матчит
    `REGISTRY_ENTRY_RE` (реестр переформатирован, например замена "N.
    check_x" на "### check_x (N)") — раньше `collect_sources_from_refs` тихо
    считал это «main без записей», и коллизия проходила в main незамеченной
    (находка ai-review PR #1201, класс #891/#893). Теперь это обязан быть
    громкий `GitError`, а не молчаливое «коллизий нет»."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(origin), str(seed)], check=True, capture_output=True)
    git("config", "user.email", "test@example.com", cwd=seed)
    git("config", "user.name", "test", cwd=seed)
    commit_file(
        seed, inv.TARGET_PATH,
        _module_stub("### check_reformatted (17)\n      не матчит REGISTRY_ENTRY_RE вовсе.\n"),
        "main: реестр переформатирован, парсер слеп",
    )
    git("push", "-u", "origin", "main", cwd=seed)
    return origin


def test_collect_sources_from_refs_raises_loud_when_main_registry_is_unreadable(tmp_path):
    origin = build_origin_with_unreadable_main_registry(tmp_path)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", str(origin), str(work)], check=True, capture_output=True)
    git("config", "user.email", "test@example.com", cwd=work)
    git("config", "user.name", "test", cwd=work)

    with pytest.raises(inv.dn.GitError, match="REGISTRY_ENTRY_RE"):
        inv.collect_sources_from_refs({"main": "main"}, inv.TARGET_PATH, cwd=work)


def test_collect_sources_from_refs_raises_git_error_on_unknown_branch(tmp_path):
    origin = build_origin_with_live_904_collision(tmp_path)
    work = clone_workdir(origin, tmp_path)

    with pytest.raises(inv.dn.GitError):
        inv.collect_sources_from_refs({"main": "does-not-exist"}, inv.TARGET_PATH, cwd=work)


def build_origin_with_intra_main_duplicate(tmp_path) -> Path:
    """main САМ несёт два инварианта под одним номером — состояние
    ПОСЛЕ того, как два независимо коллидирующих PR оба слились без
    конфликта (разные части одного файла, git не видит конфликта)."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(origin), str(seed)], check=True, capture_output=True)
    git("config", "user.email", "test@example.com", cwd=seed)
    git("config", "user.name", "test", cwd=seed)
    commit_file(
        seed, inv.TARGET_PATH,
        _module_stub(_ENTRY_17_MAIN, _ENTRY_18_MAIN, _ENTRY_19_PR_925, _ENTRY_19_PR_1121),
        "main: слиты оба претендента на 19",
    )
    git("push", "-u", "origin", "main", cwd=seed)
    return origin


def test_intra_source_duplicate_is_detected_on_real_git(tmp_path):
    origin = build_origin_with_intra_main_duplicate(tmp_path)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", str(origin), str(work)], check=True, capture_output=True)
    git("config", "user.email", "test@example.com", cwd=work)
    git("config", "user.name", "test", cwd=work)

    sources = inv.collect_sources_from_refs({"main": "main"}, inv.TARGET_PATH, cwd=work)
    violations = inv.dn.find_number_collisions(sources)

    assert len(violations) == 1
    assert violations[0]["number"] == "19"
    names = {occ["filename"] for occ in violations[0]["occurrences"]}
    assert names == {"check_ci_failure_closed_but_main_red", "check_continue_on_error_readers"}


# ── cmd_check / cmd_next: обвязка вокруг gh api и текущей ветки ────────────


def test_cmd_check_only_flags_violation_involving_current_branch(monkeypatch):
    refs = {
        "main": "main",
        "PR #1061": "agent/925-ci-run-on-main",
        "PR #1136": "agent/1121-soft-failure-digest",
        "PR #1050": "agent/1050-clean",
    }
    sources = {
        "main": {"18": ["check_pipeline_status_marker_impersonation"]},
        "PR #1061": {"19": ["check_ci_failure_closed_but_main_red"]},  # коллизия
        "PR #1136": {"19": ["check_continue_on_error_readers"]},       # коллизия
        "PR #1050": {"20": ["check_clean_addition"]},                  # чист
    }
    monkeypatch.setattr(inv.dn, "build_refs", lambda repo: refs)
    monkeypatch.setattr(inv, "collect_sources_from_refs", lambda r, path, cwd=None: sources)

    # Прогон CI чистого PR (#1050) — не участвует в коллизии, обязан быть чист.
    monkeypatch.setenv("GITHUB_HEAD_REF", "agent/1050-clean")
    monkeypatch.delenv("GITHUB_REF_NAME", raising=False)
    lines, self_name = inv.cmd_check("owner/repo")
    assert lines == []
    assert self_name == "PR #1050"

    # Прогон виновника (PR #1061) — обязан покраснеть.
    monkeypatch.setenv("GITHUB_HEAD_REF", "agent/925-ci-run-on-main")
    lines, self_name = inv.cmd_check("owner/repo")
    assert len(lines) == 1
    assert "19" in lines[0]
    assert self_name == "PR #1061"


def test_cmd_check_does_not_blame_main_for_a_foreign_open_pr_collision(monkeypatch):
    """Живой дефект, найденный при подготовке этого PR (не по аналогии —
    прогон repo-ci.yml на push в main, run 34803174089, 2026-09-14):
    `decision_numbering.cmd_check` резолвит `self_name == "main"` на
    push-событии и красит main за коллизию со СТОРОННИМ, ещё не смёрженным
    PR — main наказан за чужой долг, который сам push не создавал. Эта
    гвардия спроектирована иначе: main отвечает только за коллизию ВНУТРИ
    себя самого, не за конфликт с чужим, ещё не смёрженным номером."""
    refs = {"main": "main", "PR #944": "agent/940-artifact-retention"}
    sources = {
        "main": {"17": ["check_frontend_deploy_stale"]},
        "PR #944": {"17": ["check_ghost_actions_workflows"]},  # сторонний, ещё не смёрженный
    }
    monkeypatch.setattr(inv.dn, "build_refs", lambda repo: refs)
    monkeypatch.setattr(inv, "collect_sources_from_refs", lambda r, path, cwd=None: sources)

    # push в main — не должен покраснеть из-за чужого PR #944.
    monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
    monkeypatch.setenv("GITHUB_REF_NAME", "main")
    lines, self_name = inv.cmd_check("owner/repo")
    assert lines == []
    assert self_name == "main"

    # Но собственный прогон PR #944 обязан покраснеть — его номер, его забота.
    monkeypatch.setenv("GITHUB_HEAD_REF", "agent/940-artifact-retention")
    lines, self_name = inv.cmd_check("owner/repo")
    assert len(lines) == 1
    assert self_name == "PR #944"


def test_cmd_check_blames_main_for_an_intra_main_duplicate(monkeypatch):
    """Main САМ несёт два инварианта под одним номером (после тихого
    слияния двух коллидирующих PR) — это единственный случай, когда
    push в main обязан покраснеть."""
    refs = {"main": "main"}
    sources = {
        "main": {"19": ["check_ci_failure_closed_but_main_red", "check_continue_on_error_readers"]},
    }
    monkeypatch.setattr(inv.dn, "build_refs", lambda repo: refs)
    monkeypatch.setattr(inv, "collect_sources_from_refs", lambda r, path, cwd=None: sources)
    monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
    monkeypatch.setenv("GITHUB_REF_NAME", "main")

    lines, self_name = inv.cmd_check("owner/repo")

    assert len(lines) == 1
    assert self_name == "main"


def test_check_invariant_number_collisions_unknown_on_transport_failure(monkeypatch):
    def raising_collect_sources(repo, path=inv.TARGET_PATH, cwd=None, refs=None):
        raise inv.dn.GitError("git fetch упал")

    monkeypatch.setattr(inv, "collect_sources", raising_collect_sources)
    result = inv.check_invariant_number_collisions("owner/repo")
    assert result.status == "unknown"
    assert "git fetch" in result.reason


def test_check_invariant_number_collisions_ok_when_no_collisions(monkeypatch):
    monkeypatch.setattr(inv, "collect_sources", lambda repo, path=inv.TARGET_PATH, cwd=None, refs=None: {
        "main": {"18": ["check_pipeline_status_marker_impersonation"]},
    })
    result = inv.check_invariant_number_collisions("owner/repo")
    assert result.status == "ok"
    assert result.violations == []
