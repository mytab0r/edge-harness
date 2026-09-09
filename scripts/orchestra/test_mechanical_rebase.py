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


def pull(number, *, ref, labels=("conflict",), created_at="2026-09-01T00:00:00Z", head_repo_full_name=REPO):
    # head.repo.full_name по умолчанию = REPO (issue #764, находка ревью
    # гейта, требование 4): прод-форма для ПОДАВЛЯЮЩЕГО большинства PR — своя
    # ветка того же репозитория, не форк. Тесты фильтра ниже переопределяют
    # это поле явно.
    return {
        "number": number,
        "labels": [label(n) for n in labels],
        "created_at": created_at,
        "head": {"ref": ref, "repo": {"full_name": head_repo_full_name} if head_repo_full_name else None},
        "body": "",
    }


class FakeGh:
    """Тот же маршрутизатор, что test_scheduler.py::FakeGh — не импортируется
    оттуда (каждый тестовый файл в этом репозитории самодостаточен, тот же
    выбор, что у остальных test_*.py в scripts/orchestra/)."""

    _DEFAULT_ROUTES = {
        "actions/workflows/ai-review.yml/runs": {"workflow_runs": []},
        # По умолчанию воркер простаивает (issue #764, находка ревью гейта,
        # требование 2: process_pull теперь спрашивает sch.worker_runs_active
        # ПЕРЕД каждой попыткой) — тесты, для которых занятость воркера не
        # предмет проверки, явно переопределяют этот маршрут или монки-патчат
        # sch.worker_runs_active напрямую (см. тесты гейта ниже).
        "actions/workflows/worker.yml/runs": {"workflow_runs": []},
    }

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
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8")
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
        capture_output=True, text=True, check=True, encoding="utf-8",
    )
    return out.stdout.strip()


def main_contains(origin: Path, branch: str) -> bool:
    """True, если main является предком головы ветки — то есть ветка
    действительно перебазирована поверх текущего main, не просто изменила sha
    случайно."""
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", "refs/heads/main", f"refs/heads/{branch}"],
        cwd=origin, capture_output=True, text=True, encoding="utf-8",
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


# ── Фильтр чужих/форкнутых веток (issue #764, находка ревью гейта, требование 4) ──


def test_conflict_queue_skips_pull_from_a_fork_with_same_branch_name(monkeypatch):
    """PR из форка с СОВПАДАЮЩИМ именем ветки не должен заставить механику
    ребейзить и force-push'ить ЧУЖУЮ ветку origin этого репозитория."""
    own = pull(560, ref="agent/474-x", head_repo_full_name=REPO)
    fork = pull(562, ref="agent/474-x", head_repo_full_name="someone-else/edge-harness")
    fake = FakeGh({"issues/560/timeline?per_page=100": []})
    patch_gh(monkeypatch, fake)

    queue = mr.conflict_queue(REPO, [own, fork])

    assert [p["number"] for p in queue] == [560]


def test_conflict_queue_skips_pull_without_agent_branch_prefix(monkeypatch):
    """Ветка без префикса agent/ (в т.ч. main) — не кандидат механического
    пути, тот же структурный признак, что task-branch/#356."""
    not_agent = pull(563, ref="main", head_repo_full_name=REPO)
    fake = FakeGh({})
    patch_gh(monkeypatch, fake)

    queue = mr.conflict_queue(REPO, [not_agent])

    assert queue == []


def test_conflict_queue_skips_pull_with_missing_head_repo(monkeypatch):
    """head.repo отсутствует (None) — GitHub отдаёт это, когда исходный
    репозиторий PR удалён; тот же класс, что форк, не должен попасть в
    очередь по недосмотру."""
    orphaned = pull(564, ref="agent/474-x", head_repo_full_name=None)
    fake = FakeGh({})
    patch_gh(monkeypatch, fake)

    queue = mr.conflict_queue(REPO, [orphaned])

    assert queue == []


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


# ── Идентичность git на раннере (issue #764, находка ревью гейта, требование 1) ──


def test_attempt_rebase_reports_infra_error_not_conflict_when_git_identity_missing(tmp_path, monkeypatch):
    """Прод-форма: свежий actions/checkout НЕ ставит user.name/user.email
    (gh auth setup-git ставит только credential helper, не identity). PR
    #601 просто отстал от main (см. build_origin) — БЕЗ единого текстового
    конфликта, применяется чисто. Но `git commit` внутри `git rebase` падает
    rc=128 «unable to auto-detect email address», каталог паузы рёбейза
    заводится БЕЗ единого незаведённого пути.

    Мутационное доказательство (issue #764, находка ревью гейта, требование 1):
    откати проверку "unmerged" в attempt_rebase к старой версии (только факт
    существования .git/rebase-merge/-apply, без git diff --diff-filter=U) —
    этот тест и test_process_pull_reports_missing_identity_as_infra_error
    краснеют, потому что старый код классифицировал бы это как "conflict"
    вместо GitError (проверено вручную при подготовке этого PR: временный
    откат блока attempt_rebase к версии без проверки unmerged даёт
    `AssertionError: DID NOT RAISE mechanical_rebase.GitError` на обоих
    тестах)."""
    origin = build_origin(tmp_path)
    work = tmp_path / "work_no_identity"
    subprocess.run(["git", "clone", str(origin), str(work)], check=True, capture_output=True)
    # НЕ вызываем `git config user.email/user.name` в work — намеренно:
    # прод-форма свежего actions/checkout.
    empty_home = tmp_path / "empty_home"
    empty_home.mkdir()
    for var in (
        "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
    ):
        monkeypatch.delenv(var, raising=False)
    # HOME/USERPROFILE — на пустой каталог: без ~/.gitconfig, который на
    # машине разработчика/раннера может нести чужую identity и замаскировать
    # именно ту находку, которую воспроизводит тест (AGENTS.md: «тест кормит
    # прод-форму данных, а не пересказ» — тест обязан реально терять
    # identity, не полагаться на то, что её нет случайно).
    monkeypatch.setenv("HOME", str(empty_home))
    monkeypatch.setenv("USERPROFILE", str(empty_home))

    with pytest.raises(mr.GitError, match="identity"):
        mr.attempt_rebase(work, "agent/601-drifted-a")

    # attempt_rebase обязан отмыть working tree ПЕРЕД тем, как поднять
    # исключение (см. attempt_rebase: git rebase --abort, check=False, до
    # raise) — иначе следующий PR очереди наследует чужую паузу рёбейза
    # (issue #764, находка 5, см. тест ensure_clean_repo ниже).
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=work, capture_output=True, text=True, encoding="utf-8"
    )
    assert status.stdout.strip() == ""
    assert not (work / ".git" / "rebase-merge").exists()
    assert not (work / ".git" / "rebase-apply").exists()


def test_process_pull_reports_missing_identity_as_infra_error(tmp_path, monkeypatch):
    """Тот же сценарий на уровне process_pull (что реально видит run()):
    исход "infra-error: ...", НЕ "conflict" — иначе PR молча остался бы
    ждать агента вместо того, чтобы прод-инфраструктура починилась (fail
    loud, AGENTS.md)."""
    origin = build_origin(tmp_path)
    work = tmp_path / "work_no_identity_pp"
    subprocess.run(["git", "clone", str(origin), str(work)], check=True, capture_output=True)
    empty_home = tmp_path / "empty_home_pp"
    empty_home.mkdir()
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(empty_home))
    monkeypatch.setenv("USERPROFILE", str(empty_home))
    p = pull(601, ref="agent/601-drifted-a")
    fake = FakeGh({"issues/601/timeline?per_page=100": []})
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sch, "worker_runs_active", lambda repo: False)

    outcome = mr.process_pull(REPO, p, work)

    assert outcome.startswith("infra-error:")
    assert outcome != "conflict"


# ── Взаимное исключение с агентским путём (issue #764, находка ревью гейта, требование 2) ──


def test_process_pull_defers_when_worker_is_active(tmp_path, monkeypatch):
    origin = build_origin(tmp_path)
    work = clone_workdir(origin, tmp_path)
    p = pull(601, ref="agent/601-drifted-a")
    original_tip = branch_tip(origin, "agent/601-drifted-a")
    fake = FakeGh({"issues/601/timeline?per_page=100": []})
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sch, "worker_runs_active", lambda repo: True)

    outcome = mr.process_pull(REPO, p, work)

    assert outcome == "worker-running"
    assert branch_tip(origin, "agent/601-drifted-a") == original_tip  # head не тронут


def test_run_reports_worker_running_and_skips_push(tmp_path, monkeypatch):
    """Мутационное доказательство (issue #764, находка ревью гейта, требование 2):
    без этого гейта (monkeypatch.setattr(sch, "worker_runs_active", lambda repo: False))
    PR #601 получил бы "resolved" и запушенную ветку — тест ниже покраснел бы на
    строке assert outcomes[601] == "resolved", доказывая, что гейт — не no-op."""
    origin = build_origin(tmp_path)
    work = clone_workdir(origin, tmp_path)
    p = pull(601, ref="agent/601-drifted-a")
    original_tip = branch_tip(origin, "agent/601-drifted-a")
    fake = FakeGh({"pulls?state=open": [p], "issues/601/timeline?per_page=100": []})
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sch, "worker_runs_active", lambda repo: True)

    lines, outcomes = mr.run(REPO, work)

    assert outcomes[601] == "worker-running"
    assert branch_tip(origin, "agent/601-drifted-a") == original_tip
    assert any("worker.yml" in line for line in lines)


# ── Каскад после неудачного abort (issue #764, находка ревью гейта, требование 5) ──


def test_ensure_clean_repo_recovers_leftover_rebase_state_for_next_pull(tmp_path, monkeypatch):
    """Симулирует ровно найденный дефект: предыдущая итерация оставила
    рабочее дерево с НЕЗАВЕРШЁННЫМ (не отменённым) рёбейзом — реальный
    текстовый конфликт, `git rebase --abort` НЕ вызван (как если бы он сам
    упал). Без ensure_clean_repo следующий PR очереди получил бы
    "you need to resolve your current index first" при попытке checkout —
    не потому, что у НЕГО есть конфликт."""
    origin = build_origin(tmp_path)
    work = clone_workdir(origin, tmp_path)

    # Заводим реальную паузу рёбейза руками (без abort) — воспроизводит
    # состояние "предыдущая итерация не отмылась".
    subprocess.run(["git", "fetch", "origin", "main", "agent/604-conflict-d"], cwd=work, check=True, capture_output=True)
    subprocess.run(
        ["git", "checkout", "-B", "agent/604-conflict-d", "origin/agent/604-conflict-d"],
        cwd=work, check=True, capture_output=True,
    )
    result = subprocess.run(["git", "rebase", "origin/main"], cwd=work, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode != 0
    assert (work / ".git" / "rebase-merge").exists() or (work / ".git" / "rebase-apply").exists()

    # Следующий PR очереди — реально резолвящийся (agent/601), но дерево
    # ещё несёт чужую паузу рёбейза.
    outcome = mr.attempt_rebase(work, "agent/601-drifted-a")

    assert outcome == "resolved"
    assert not (work / ".git" / "rebase-merge").exists()
    assert not (work / ".git" / "rebase-apply").exists()


# ── Полный отказ очереди — ненулевой исход (issue #764, находка ревью гейта, требование 5) ──


def test_failed_count_counts_only_infra_error_outcomes():
    outcomes = {601: "resolved", 604: "conflict", 609: "infra-error: boom", 610: "infra-error: boom2"}

    assert mr.failed_count(outcomes) == 2


def test_all_infra_error_outcomes_is_detectable_as_total_failure():
    """Мутационное доказательство требования 5 (issue #762): main() читает
    именно `failed_count(outcomes) == len(outcomes)` — если бы вместо этого
    условие сравнивало с 0 (старое поведение, всегда `return 0`), этот тест
    остался бы зелёным, но живой прогон main() — красным. Живой прогон
    main() — см. test_main_returns_nonzero_when_all_outcomes_are_infra_error
    ниже (issue #764, находка приёмки: требование выше проверяло только
    failed_count() напрямую, ни разу не гоняя сам main())."""
    outcomes = {609: "infra-error: a", 610: "infra-error: b"}

    assert mr.failed_count(outcomes) == len(outcomes)


# ── main(): живой прогон — исход не только failed_count() напрямую, но и код возврата ──
# issue #764, находка приёмки: до этого раздела ни один тест не гонял main()
# целиком, «живой прогон main() был проверен вручную» оставался утверждением
# без носителя. run() подменяется monkeypatch — это уже отдельно проверенный
# слой (тесты выше и test_run_* ниже), здесь предмет — именно условие
# ненулевого выхода внутри main().


def _patch_main_environment(monkeypatch, *, repo="o/r"):
    """Общий каркас для тестов main(): CI-маркеры (см. _require_ci_environment
    ниже) + гашение summary (побочный эффект печати в step summary — не
    предмет этих тестов)."""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_RUN_ID", "123456")
    monkeypatch.setenv("GITHUB_REPOSITORY", repo)
    monkeypatch.setattr(sch, "summary", lambda lines: None)


def test_main_returns_nonzero_when_all_outcomes_are_infra_error(monkeypatch):
    _patch_main_environment(monkeypatch)
    monkeypatch.setattr(
        mr, "run",
        lambda repo, repo_dir: (["…"], {609: "infra-error: a", 610: "infra-error: b"}),
    )

    assert mr.main() == 1


def test_main_returns_zero_on_mixed_outcomes(monkeypatch):
    """Смесь resolved/infra-error — очередь не ПОЛНОСТЬЮ провалена, код 0."""
    _patch_main_environment(monkeypatch)
    monkeypatch.setattr(
        mr, "run",
        lambda repo, repo_dir: (["…"], {601: "resolved", 609: "infra-error: a"}),
    )

    assert mr.main() == 0


def test_main_returns_zero_on_all_worker_running_queue(monkeypatch):
    """Очередь целиком отложена (воркер занят) — не infra-error ни разу,
    код 0: тормоз без газа здесь не про main(), это отдельный такт."""
    _patch_main_environment(monkeypatch)
    monkeypatch.setattr(
        mr, "run",
        lambda repo, repo_dir: (["…"], {601: "worker-running", 602: "worker-running"}),
    )

    assert mr.main() == 0


def test_main_returns_zero_on_empty_queue(monkeypatch):
    """Нет ни одного PR с меткой conflict — outcomes пуст, `outcomes and …`
    короткозамыкает на False, код 0 (не 1 на пустой очереди)."""
    _patch_main_environment(monkeypatch)
    monkeypatch.setattr(mr, "run", lambda repo, repo_dir: (["…"], {}))

    assert mr.main() == 0


# ── main() отказывается работать вне CI (issue #764, находка приёмки) ───────
# ensure_clean_repo безусловно сносит рабочее дерево (rebase --abort,
# reset --hard, clean -fd) — приёмка воспроизвела живьём снос незакоммиченных
# правок и незавершённого рёбейза человека при ручном запуске. `run()`
# подменяется заглушкой, которая падает, если её вообще позвали — так тест
# доказывает, что гвардия срабатывает ДО касания репозитория, а не просто
# существует где-то рядом.


def _run_must_not_be_called(repo, repo_dir):
    raise AssertionError("run() не должен звонить — гвардия обязана остановить main() раньше")


def test_main_refuses_outside_ci_when_both_markers_are_missing(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setattr(mr, "run", _run_must_not_be_called)

    with pytest.raises(RuntimeError, match="GitHub Actions"):
        mr.main()


def test_main_refuses_when_only_github_actions_flag_is_set(monkeypatch):
    """Единственный признак GITHUB_ACTIONS недостаточен: .githooks/pre-commit
    использует ЭТУ ЖЕ переменную РОВНО НАОБОРОТ (её наличие ОТКЛЮЧАЕТ гвардию
    хука) и сам называет риск, что она утечёт в локальное окружение
    (например, при работе с `gh`/`act`). Без GITHUB_RUN_ID как второго,
    независимого признака такая утечка обошла бы защиту этого модуля тоже."""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setattr(mr, "run", _run_must_not_be_called)

    with pytest.raises(RuntimeError, match="GitHub Actions"):
        mr.main()


def test_main_refuses_when_only_github_run_id_is_set(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setenv("GITHUB_RUN_ID", "123456")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setattr(mr, "run", _run_must_not_be_called)

    with pytest.raises(RuntimeError, match="GitHub Actions"):
        mr.main()


def test_main_proceeds_when_both_ci_markers_are_present(monkeypatch):
    """Позитивный полюс: оба признака выставлены (как их выставляет сам
    раннер Actions на старте job'а) — гвардия не мешает нормальному ходу."""
    _patch_main_environment(monkeypatch)
    monkeypatch.setattr(mr, "run", lambda repo, repo_dir: (["…"], {}))

    assert mr.main() == 0


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
