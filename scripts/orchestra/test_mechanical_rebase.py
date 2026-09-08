#!/usr/bin/env python3
"""Тесты дешёвого механического ребейза конфликтных PR (issue #762).

Прод-форма для GitHub API — тот же приём, что test_scheduler.py (FakeGh,
маршрутизация по подстроке пути). Прод-форма для git — НАСТОЯЩИЙ git поверх
временного локального bare-репозитория ("origin"): рёбейз/конфликт здесь не
мокается, а действительно выполняется тем же subprocess'ом `git`, что и в
проде (AGENTS.md, «тест кормит прод-форму данных, а не пересказ» — пересказ
git-конфликта строкой было бы тем самым запрещённым классом).

Сценарий из задачи #762 (пять конфликтных PR, три сходятся ребейзом, два
нет) — test_run_resolves_mechanical_conflicts_and_leaves_real_ones_for_agent
ниже. Мутация (verified вручную, см. PR): откати process_pull к заглушке,
которая всегда возвращает "conflict" — тест краснеет (0 вместо 3 resolved).

Запуск: python -m pytest scripts/orchestra/test_mechanical_rebase.py -q
"""

import subprocess
import sys
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))  # scheduler.py делает `from pulse_guard import …`

# ВАЖНО: plain `import`, не importlib.util.spec_from_file_location (в отличие
# от test_scheduler.py) — mechanical_rebase.py само делает `import scheduler
# as sch` тем же способом. Если бы тест грузил scheduler.py ВТОРЫМ, отдельным
# module_from_spec (как test_scheduler.py), это была бы ВТОРАЯ копия модуля
# scheduler — patch_gh(monkeypatch, fake) патчил бы sch.gh теста, а
# mechanical_rebase.run() внутри себя дёргал бы СВОЙ, непатченный sch.gh
# (реальный `gh api`, живая сеть) — находка этого же PR (живой прогон тестов
# упирался в реальный GitHub CLI ровно по этой причине). `import` с общим
# sys.modules["scheduler"] — единственный способ, которым тест и модуль под
# тестом разделяют ОДИН и тот же патченный объект gh.
import scheduler as sch  # noqa: E402
import mechanical_rebase as mr  # noqa: E402


REPO = "mytab0r/edge-harness"


# ── Prod-форма GitHub API: тот же паттерн, что test_scheduler.py ────────────


def label(name):
    return {"id": "LA_kwDOUHBaqc8AAAACypPLSQ", "name": name, "description": "", "color": "0E8A16"}


def pull(number, *, ref, labels=("conflict",), created_at="2026-09-01T00:00:00Z"):
    return {
        "number": number,
        "labels": [label(n) for n in labels],
        "created_at": created_at,
        "head": {"ref": ref},
        "body": "",
    }


class FakeGh:
    """Тот же маршрутизатор, что test_scheduler.py::FakeGh — не импортируется
    оттуда (каждый тестовый файл в этом репозитории самодостаточен, тот же
    выбор, что у остальных test_*.py в scripts/orchestra/)."""

    _DEFAULT_ROUTES = {"actions/workflows/ai-review.yml/runs": {"workflow_runs": []}}

    def __init__(self, routes: dict):
        self.routes = {**self._DEFAULT_ROUTES, **routes}
        self.calls: list[str] = []

    def __call__(self, *args):
        joined = " ".join(args)
        self.calls.append(joined)
        # Самый длинный (самый специфичный) совпавший фрагмент побеждает —
        # без этого дефолтный короткий фрагмент ai-review.yml/runs (без
        # ?status=…) перехватывал бы вызов раньше специфичного маршрута
        # теста (…?status=in_progress), заведённого ПОСЛЕ дефолта в словаре
        # (порядок вставки, не длина, иначе решал бы порядок словаря).
        matches = [(fragment, result) for fragment, result in self.routes.items() if fragment in joined]
        if not matches:
            raise AssertionError(f"нет маршрута для: {joined}")
        _, result = max(matches, key=lambda item: len(item[0]))
        if isinstance(result, Exception):
            raise result
        return result


def patch_gh(monkeypatch, fake):
    monkeypatch.setattr(sch, "gh", fake)


# ── Real git: bare "origin" + рабочее дерево, как actions/checkout@v7 ───────


def git(*args, cwd):
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, f"git {args} упал в {cwd}: {result.stderr}"
    return result.stdout


def commit_file(repo_dir, name, content, message):
    (repo_dir / name).write_text(content, encoding="utf-8")
    git("add", name, cwd=repo_dir)
    git("commit", "-m", message, cwd=repo_dir)


def build_origin(tmp_path) -> Path:
    """origin.git — bare-репозиторий, играющий роль GitHub-стороны. main +
    5 веток, две из которых МЕХАНИЧЕСКИ конфликтуют с main (правят ту же
    строку того же файла), три — просто отстали (main ушёл в другой файл)."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(origin), str(seed)], check=True, capture_output=True)
    git("config", "user.email", "test@example.com", cwd=seed)
    git("config", "user.name", "test", cwd=seed)
    commit_file(seed, "shared.txt", "line1\n", "base: shared.txt")
    commit_file(seed, "README.md", "hello\n", "base: readme")
    git("push", "-u", "origin", "main", cwd=seed)
    base_sha = git("rev-parse", "HEAD", cwd=seed).strip()

    branches = {
        # Резолвится механически: свой отдельный файл, main его не трогает.
        "agent/601-drifted-a": ("feature-601.txt", "a\n", False),
        "agent/602-drifted-b": ("feature-602.txt", "b\n", False),
        "agent/603-drifted-c": ("feature-603.txt", "c\n", False),
        # Настоящий конфликт: та же строка shared.txt, что правит main ниже.
        "agent/604-conflict-d": ("shared.txt", "line1-branch-d\n", True),
        "agent/605-conflict-e": ("shared.txt", "line1-branch-e\n", True),
    }
    for branch, (filename, content, overwrite) in branches.items():
        git("checkout", "-b", branch, base_sha, cwd=seed)
        if overwrite:
            (seed / filename).write_text(content, encoding="utf-8")
            git("add", filename, cwd=seed)
            git("commit", "-m", f"{branch}: edit {filename}", cwd=seed)
        else:
            commit_file(seed, filename, content, f"{branch}: add {filename}")
        git("push", "-u", "origin", branch, cwd=seed)
        git("checkout", "main", cwd=seed)

    # main уходит вперёд: один безобидный коммит (README) + один коммит,
    # который меняет ИМЕННО ту строку shared.txt, что правили d/e — вот этот
    # коммит и есть источник настоящего конфликта для d/e, и он же безвреден
    # для a/b/c (они shared.txt не касались вовсе).
    commit_file(seed, "README.md", "hello\nmain moved on\n", "main: drift readme")
    (seed / "shared.txt").write_text("line1-main\n", encoding="utf-8")
    git("add", "shared.txt", cwd=seed)
    git("commit", "-m", "main: edit shared.txt", cwd=seed)
    git("push", "origin", "main", cwd=seed)
    return origin


def clone_workdir(origin: Path, tmp_path: Path) -> Path:
    work = tmp_path / "work"
    subprocess.run(["git", "clone", str(origin), str(work)], check=True, capture_output=True)
    git("config", "user.email", "test@example.com", cwd=work)
    git("config", "user.name", "test", cwd=work)
    return work


def branch_tip(origin: Path, branch: str) -> str:
    out = subprocess.run(
        ["git", "rev-parse", f"refs/heads/{branch}"], cwd=origin,
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def main_contains(origin: Path, branch: str) -> bool:
    """True, если main является предком головы ветки — то есть ветка
    действительно перебазирована поверх текущего main, не просто изменила sha
    случайно."""
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", "refs/heads/main", f"refs/heads/{branch}"],
        cwd=origin, capture_output=True, text=True,
    )
    return result.returncode == 0


# ── conflict_queue: обход от старейшего эпизода к новейшему (issue #588) ────


def test_conflict_queue_orders_by_conflict_labeled_at_oldest_first(monkeypatch):
    p_new = pull(561, ref="agent/475-y", created_at="2026-09-02T00:00:00Z")
    p_old = pull(560, ref="agent/474-x", created_at="2026-09-01T00:00:00Z")
    fake = FakeGh({
        "issues/561/timeline?per_page=100": [],
        "issues/560/timeline?per_page=100": [],
    })
    patch_gh(monkeypatch, fake)

    queue = mr.conflict_queue(REPO, [p_new, p_old])

    assert [p["number"] for p in queue] == [560, 561]


def test_conflict_queue_skips_pulls_without_conflict_label(monkeypatch):
    conflicted = pull(560, ref="agent/474-x")
    clean = pull(561, ref="agent/475-y", labels=("review:ok",))
    fake = FakeGh({"issues/560/timeline?per_page=100": []})
    patch_gh(monkeypatch, fake)

    queue = mr.conflict_queue(REPO, [conflicted, clean])

    assert [p["number"] for p in queue] == [560]


# ── Живой git: attempt_rebase/push_rebased по-настоящему ────────────────────


def test_attempt_rebase_resolves_when_branches_only_drifted(tmp_path):
    origin = build_origin(tmp_path)
    work = clone_workdir(origin, tmp_path)

    outcome = mr.attempt_rebase(work, "agent/601-drifted-a")

    assert outcome == "resolved"
    # working tree сейчас на перебазированной ветке — main её предок.
    log = git("log", "--oneline", "origin/main..HEAD", cwd=work)
    assert log.strip() != ""  # свой коммит остался поверх main


def test_attempt_rebase_detects_structural_conflict_and_aborts(tmp_path):
    origin = build_origin(tmp_path)
    work = clone_workdir(origin, tmp_path)

    outcome = mr.attempt_rebase(work, "agent/604-conflict-d")

    assert outcome == "conflict"
    # git rebase --abort уже случился внутри attempt_rebase — working tree чист.
    status = git("status", "--porcelain", cwd=work)
    assert status.strip() == ""
    assert not (work / ".git" / "rebase-merge").exists()
    assert not (work / ".git" / "rebase-apply").exists()


def test_attempt_rebase_raises_git_error_for_missing_branch(tmp_path):
    origin = build_origin(tmp_path)
    work = clone_workdir(origin, tmp_path)

    with pytest.raises(mr.GitError):
        mr.attempt_rebase(work, "agent/999-does-not-exist")


# ── run(): полный сценарий задачи #762 — 5 PR, 3 сходятся, 2 нет ────────────


def test_run_resolves_mechanical_conflicts_and_leaves_real_ones_for_agent(tmp_path, monkeypatch):
    origin = build_origin(tmp_path)
    work = clone_workdir(origin, tmp_path)

    resolvable = [601, 602, 603]
    conflicting = [604, 605]
    refs = {
        601: "agent/601-drifted-a", 602: "agent/602-drifted-b", 603: "agent/603-drifted-c",
        604: "agent/604-conflict-d", 605: "agent/605-conflict-e",
    }
    pulls = [
        pull(n, ref=refs[n], created_at=f"2026-09-0{i + 1}T00:00:00Z")
        for i, n in enumerate(sorted(refs))
    ]
    original_tips = {n: branch_tip(origin, refs[n]) for n in refs}

    routes = {"pulls?state=open": pulls}
    for n in refs:
        routes[f"issues/{n}/timeline?per_page=100"] = []
    fake = FakeGh(routes)
    patch_gh(monkeypatch, fake)

    lines, outcomes = mr.run(REPO, work)

    for n in resolvable:
        assert outcomes[n] == "resolved", f"PR #{n}: {outcomes[n]}"
        assert branch_tip(origin, refs[n]) != original_tips[n]  # реально запушено
        assert main_contains(origin, refs[n])  # main теперь предок головы ветки
    for n in conflicting:
        assert outcomes[n] == "conflict", f"PR #{n}: {outcomes[n]}"
        assert branch_tip(origin, refs[n]) == original_tips[n]  # НЕ тронуто

    # Ни один рубль агентского бюджета не потрачен этим модулем — он вообще
    # не знает про worker.yml/дефекты попыток (см. докстринг: снятие метки и
    # dispatch — не его забота).
    assert not any("worker.yml/dispatches" in c for c in fake.calls)
    assert not any("assignees" in c and c.startswith("-X DELETE") for c in fake.calls)
    assert any(f"{len(refs)} PR в очереди" in line for line in lines)
    assert sum(1 for line in lines if line.startswith("✅")) == 3
    assert sum(1 for line in lines if line.startswith("🔀")) == 2


def test_run_defers_pr_with_active_ai_review(tmp_path, monkeypatch):
    origin = build_origin(tmp_path)
    work = clone_workdir(origin, tmp_path)
    p = pull(601, ref="agent/601-drifted-a")
    original_tip = branch_tip(origin, "agent/601-drifted-a")
    fake = FakeGh({
        "pulls?state=open": [p],
        "issues/601/timeline?per_page=100": [],
        "actions/workflows/ai-review.yml/runs?status=in_progress": {
            "workflow_runs": [{"id": 1, "display_title": "ai-review PR #601"}]
        },
    })
    patch_gh(monkeypatch, fake)

    lines, outcomes = mr.run(REPO, work)

    assert outcomes[601] == "ai-review-running"
    assert branch_tip(origin, "agent/601-drifted-a") == original_tip  # head не тронут
    assert any("ai-review.yml" in line for line in lines)


def test_run_reports_infra_error_separately_from_conflict(tmp_path, monkeypatch):
    # Мутация-проверка требования 4 (fail loud): ветка, которой не существует
    # на origin, — это НЕ "конфликт не сошёлся", это отдельная причина
    # (branch missing) и обязана называться отдельной строкой.
    origin = build_origin(tmp_path)
    work = clone_workdir(origin, tmp_path)
    p = pull(609, ref="agent/609-missing-branch")
    fake = FakeGh({
        "pulls?state=open": [p],
        "issues/609/timeline?per_page=100": [],
    })
    patch_gh(monkeypatch, fake)

    lines, outcomes = mr.run(REPO, work)

    assert outcomes[609].startswith("infra-error:")
    assert outcomes[609] != "conflict"
    assert any(line.startswith("⚠️") for line in lines)
    assert not any(line.startswith("🔀") for line in lines)


# ── Мутация #5 (задача #762): без мех-ребейза оба класса PR идут агенту ─────


def test_mutation_disabling_mechanical_path_sends_everything_to_agent(tmp_path, monkeypatch):
    """Не тест самого модуля — доказательство того, ЧТО он меняет. Если
    process_pull всегда возвращает "conflict" (эквивалент «расшивка вернулась
    на единственный агентский путь», как было до задачи #762), НИ ОДИН из
    трёх реально резолвящихся PR не получает обновлённую ветку — три
    "resolved" из живого сценария превращаются в ноль. Мутация обязана
    покраснить основной тест выше; здесь — прямая проверка того же факта на
    заглушке, чтобы диагноз был читаем без диффа."""
    origin = build_origin(tmp_path)
    work = clone_workdir(origin, tmp_path)
    refs = {601: "agent/601-drifted-a", 604: "agent/604-conflict-d"}
    pulls = [pull(n, ref=refs[n]) for n in refs]
    fake = FakeGh({
        "pulls?state=open": pulls,
        **{f"issues/{n}/timeline?per_page=100": [] for n in refs},
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(mr, "process_pull", lambda repo, pull, repo_dir: "conflict")

    _, outcomes = mr.run(REPO, work)

    assert outcomes == {601: "conflict", 604: "conflict"}  # PR #601 больше не резолвится
