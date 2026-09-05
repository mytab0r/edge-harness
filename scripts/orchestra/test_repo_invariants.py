#!/usr/bin/env python3
"""Тесты инвариантов состояния репозитория (scripts/orchestra/repo_invariants.py, #244).

Кормятся прод-формой: тела issue/PR ниже — реальный текст, снятый живым
`gh api` по этому репозиторию 2026-09-03 (см. PR #244) — не пересказ. Пять
инвариантов, каждый доказан мутацией (снять фикс — тест краснеет), плюс
гвардия холостого хода: на здоровом снимке ни один инвариант не срабатывает
и build_report не делает ни одного мутирующего вызова.

Запуск: python -m pytest scripts/orchestra/test_repo_invariants.py -q
"""

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parent
SCRIPT = _DIR / "repo_invariants.py"
spec = importlib.util.spec_from_file_location("repo_invariants", SCRIPT)
ri = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ri)  # type: ignore[union-attr]


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def task_issue(number, title="", issue_body="", assignees=()):
    # Параметр НЕ называется body= — та же гвардия класса #124
    # (scripts/orchestra/, scripts/lib/, scripts/review/: grep ',\s*body='
    # ловит keyword-вызов gh()) текстово матчит и сигнатуру функции с
    # дефолтом body="" — ложное срабатывание, не связанное с gh() вовсе
    # (тот же приём уже применён в test_scheduler.py::pull → pr_body).
    return {
        "number": number,
        "title": title,
        "body": issue_body,
        "assignees": [{"login": a} for a in assignees],
        "labels": [{"name": "task"}],
    }


def merged_pr(number, pr_body, merged_at):
    return {"number": number, "body": pr_body, "merged_at": merged_at}


def open_pr(number, pr_body="", labels=()):
    return {"number": number, "body": pr_body, "labels": [{"name": n} for n in labels]}


# ══════════════════════════════════════════════════════════════════════════
# Инвариант 1: задача открыта без исполнителя, PR уже слит
# ══════════════════════════════════════════════════════════════════════════


def test_primary_declared_task_requires_bare_line():
    assert ri.primary_declared_task("#18\n\nтекст") == 18
    assert ri.primary_declared_task("#205") == 205
    # живая находка PR #137: перенос строки внутри прозы уронил "#119 из
    # пула..." на отдельную строку — это НЕ декларация, реальная первая
    # строка тела — "#18"
    assert ri.primary_declared_task(
        "#18\n\nAI-ревьюер...\n\n"
        "`gather` прогнан на живом PR #130: промпт собран с телом задачи\n"
        "#119 из пула, meta.json с head — выверено."
    ) == 18
    assert ri.primary_declared_task("## Что сделано\n#18") is None  # заголовок первой строкой — не декларация
    assert ri.primary_declared_task("") is None
    assert ri.primary_declared_task(None) is None


def test_reopened_after_merge_flags_free_task_with_merged_pr():
    tasks = [task_issue(18, "AI-ревьюер диффа", assignees=())]
    pulls = [merged_pr(137, "#18\n\nтекст", "2026-08-31T17:46:11Z"),
             merged_pr(138, "#18\n\nвторой заход", "2026-09-02T21:31:47Z")]
    violations = ri.check_reopened_after_merge(tasks, pulls)
    assert len(violations) == 1
    assert violations[0]["issue"] == 18
    assert violations[0]["prs"] == [137, 138]
    assert violations[0]["merged_at"] == "2026-09-02T21:31:47Z"  # самый свежий


def test_reopened_after_merge_silent_when_assigned():
    # тот же слитый PR, но задача СЕЙЧАС занята исполнителем — норма
    # (пост-мерж проверка ещё не сделана, это не бросили)
    tasks = [task_issue(18, assignees=("mytab0r",))]
    pulls = [merged_pr(137, "#18", "2026-08-31T17:46:11Z")]
    assert ri.check_reopened_after_merge(tasks, pulls) == []


def test_reopened_after_merge_silent_without_merged_pr():
    tasks = [task_issue(18, assignees=())]
    pulls = [merged_pr(999, "#77", "2026-08-31T17:46:11Z")]  # чужая декларация
    assert ri.check_reopened_after_merge(tasks, pulls) == []


def test_reopened_after_merge_mutation_guard():
    # Мутация: если бы проверка не сверялась с unassigned (снят фильтр по
    # исполнителю), КАЖДАЯ задача со слитым PR стала бы «нарушением» — на
    # живом репозитории это стандартный кратковременный путь после мержа,
    # а не баг. Тест доказывает, что фильтр обязателен.
    tasks = [task_issue(18, assignees=("mytab0r",))]
    pulls = [merged_pr(137, "#18", "2026-08-31T17:46:11Z")]
    assert ri.check_reopened_after_merge(tasks, pulls) == []
    tasks_unassigned = [task_issue(18, assignees=())]
    assert len(ri.check_reopened_after_merge(tasks_unassigned, pulls)) == 1


# ══════════════════════════════════════════════════════════════════════════
# Инвариант 2 — выведен из состава (см. блок-комментарий в
# repo_invariants.py на месте бывшего check_free_task_count_mismatch):
# #247 закрыл класс substring-scan, который он ловил, сравнивать стало
# не с чем. Тестов для отсутствующей функции нет.
# ══════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════
# Инвариант 3: review:ok без ai:* дольше порога
# ══════════════════════════════════════════════════════════════════════════


class FakeGh:
    """Тот же маршрутизатор, что test_scheduler.py::FakeGh — подстрока пути
    → прод-форма ответа; фиксирует вызовы для гвардии холостого хода."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, *args):
        joined = " ".join(args)
        self.calls.append(joined)
        for fragment, result in self.routes.items():
            if fragment in joined:
                if isinstance(result, Exception):
                    raise result
                return result
        raise AssertionError(f"нет маршрута для: {joined}")

    def mutating_calls(self):
        return [c for c in self.calls if c.startswith(("-X POST", "-X PUT", "-X DELETE"))]


def patch_gh(monkeypatch, fake):
    """Единая точка патча — оба модуля читают gh() по имени (repo_invariants
    реэкспортирует pulse_guard.gh как свой атрибут `ri.gh`, но
    post_issue_comment/escalate внутри pulse_guard.py вызывают СВОЙ
    module-level `gh`, а не `ri.gh`). Патчить только `ri.gh` недостаточно —
    так один прогон реально ушёл в живой issue #120 (инцидент этой задачи,
    #244: очищено вручную, gh api -X DELETE .../comments/5527288512).
    Патчим оба имени — тот же приём, что test_scheduler.py::patch_gh."""
    monkeypatch.setattr(ri, "gh", fake)
    monkeypatch.setattr(ri.pulse_guard, "gh", fake)


def timeline_with_review_ok(when: str):
    return [{"event": "labeled", "label": {"name": "review:ok"}, "created_at": when}]


def test_stuck_review_gate_flags_after_threshold(monkeypatch):
    pull = open_pr(246, labels=["review:ok"])
    fake = FakeGh({"issues/246/timeline": timeline_with_review_ok("2026-09-01T10:00:00Z")})
    patch_gh(monkeypatch, fake)
    now = utc(2026, 9, 3, 14, 0)  # заведомо больше порога 120 мин
    violations = ri.check_stuck_review_gate("mytab0r/edge-harness", now, [pull])
    assert len(violations) == 1
    assert violations[0]["pr"] == 246


def test_stuck_review_gate_silent_within_threshold(monkeypatch):
    pull = open_pr(246, labels=["review:ok"])
    fake = FakeGh({"issues/246/timeline": timeline_with_review_ok("2026-09-03T14:07:04Z")})
    patch_gh(monkeypatch, fake)
    now = utc(2026, 9, 3, 14, 13)  # 6 минут — живой случай PR #246 на 2026-09-03
    assert ri.check_stuck_review_gate("mytab0r/edge-harness", now, [pull]) == []


def test_stuck_review_gate_silent_when_verdict_present(monkeypatch):
    pull = open_pr(163, labels=["review:ok", "ai:changes-requested"])
    fake = FakeGh({})  # таймлайн даже не должен запрашиваться
    patch_gh(monkeypatch, fake)
    now = utc(2026, 9, 3, 14, 0)
    assert ri.check_stuck_review_gate("mytab0r/edge-harness", now, [pull]) == []
    assert fake.calls == []


def test_stuck_review_gate_mutation_guard(monkeypatch):
    # Мутация: тот же PR/таймлайн, порог опущен ниже возраста — обязан
    # появиться как нарушение (реальная мутация значения, не проверка > 0,
    # находка AI-ревью PR #249: старый вариант не краснел на снятии фикса).
    pull = open_pr(246, labels=["review:ok"])
    fake = FakeGh({"issues/246/timeline": timeline_with_review_ok("2026-09-03T14:07:04Z")})
    patch_gh(monkeypatch, fake)
    now = utc(2026, 9, 3, 14, 13)  # 6 минут — в пределах порога 120 (см. silent_within_threshold)
    assert ri.check_stuck_review_gate("mytab0r/edge-harness", now, [pull]) == []
    monkeypatch.setattr(ri, "UNHEALTHY_PR_AFTER_MINUTES", 1)
    violations = ri.check_stuck_review_gate("mytab0r/edge-harness", now, [pull])
    assert len(violations) == 1


# ══════════════════════════════════════════════════════════════════════════
# Инвариант 4: openspec/changes полностью отмечен и не заархивирован
# ══════════════════════════════════════════════════════════════════════════


def write_tasks_md(tmp_path, name, content):
    change_dir = tmp_path / name
    change_dir.mkdir(parents=True, exist_ok=True)
    (change_dir / "tasks.md").write_text(content, encoding="utf-8")
    return change_dir


def test_unarchived_complete_flags_fully_checked(tmp_path):
    write_tasks_md(tmp_path, "walking-skeleton", "- [x] один\n- [x] два\n")
    violations = ri.check_unarchived_complete_changes(tmp_path)
    assert violations == [{"change": "walking-skeleton", "checked": 2}]


def test_unarchived_complete_silent_when_box_unchecked(tmp_path):
    write_tasks_md(tmp_path, "ai-review-gate", "- [x] один\n- [ ] два — в пуле\n")
    assert ri.check_unarchived_complete_changes(tmp_path) == []


def test_unarchived_complete_silent_when_no_checkboxes(tmp_path):
    write_tasks_md(tmp_path, "dsh-pulse-self-update", "просто текст без чекбоксов\n")
    assert ri.check_unarchived_complete_changes(tmp_path) == []


def test_unarchived_complete_ignores_archive_dir(tmp_path):
    write_tasks_md(tmp_path / "archive", "already-done", "- [x] всё\n")
    assert ri.check_unarchived_complete_changes(tmp_path) == []


def test_unarchived_complete_mutation_guard(tmp_path):
    write_tasks_md(tmp_path, "walking-skeleton", "- [x] один\n- [x] два\n")
    assert len(ri.check_unarchived_complete_changes(tmp_path)) == 1
    # Реальная мутация: каталог, буквально названный "archive", с полностью
    # отмеченным tasks.md прямо внутри него (не под ним) — фильтр
    # `entry.name == "archive"` обязан его исключить. Снять фильтр (удалить
    # условие `or entry.name == "archive"` в repo_invariants.py) — тест ниже
    # покраснеет: без фильтра "archive" стал бы обычной записью с checked=1.
    write_tasks_md(tmp_path, "archive", "- [x] один\n")
    violations = ri.check_unarchived_complete_changes(tmp_path)
    assert all(v["change"] != "archive" for v in violations)


# ══════════════════════════════════════════════════════════════════════════
# Инвариант 5: пересекающаяся улика file:line у двух открытых задач
# ══════════════════════════════════════════════════════════════════════════

# Реальные тела issue #202/#213 (обрезаны до релевантных фрагментов), сняты
# gh api 2026-09-03. Живой класс: contract_check.py разобран трижды под
# разными номерами.
ISSUE_202_BODY = """## Факты

- `scripts/orchestra/contract_check.py:129-142` — конфликтом считается любой
  другой открытый PR, в теле которого встречается `#{issue_number}`.
"""

ISSUE_213_BODY = """## Где именно (file:line)

- `scripts/orchestra/contract_check.py:103-110` — извлечение issue_number
  ПРОВЕРЯЕМОГО PR.
- `scripts/orchestra/contract_check.py:139-152` — извлечение issue_number
  у ЧУЖИХ PR при поиске конфликта.
"""

# Реальное тело issue #204/#217 (обрезано) — оба трогают check_pr.py, но по
# РАЗНЫМ поводам и БЕЗ пересекающихся строк — не должны склеиваться.
ISSUE_204_BODY = """## Факты

- `scripts/review/check_pr.py:114-125` — при диффе больше порога ставится
  `review:large`, слияние требует ещё и `review:large-ok`.
"""

ISSUE_217_BODY = """## Что нужно

Проверка в детерминированном гейте (`scripts/review/check_pr.py` — там уже
считается размер диффа и ставятся вердикт-метки), падающая громко.
"""


def test_extract_locators_line_form():
    locs = ri.extract_locators(ISSUE_202_BODY)
    assert ("scripts/orchestra/contract_check.py", 129, 142) in locs


def test_duplicate_evidence_catches_202_213_overlap():
    tasks = [
        task_issue(202, "Контракт не различает эпик и лист", ISSUE_202_BODY),
        task_issue(213, "contract: асимметричное распознавание", ISSUE_213_BODY),
    ]
    violations = ri.check_duplicate_evidence(tasks)
    assert len(violations) == 1
    assert violations[0]["issues"] == [202, 213]
    assert "contract_check.py" in violations[0]["shared_location"]


def test_duplicate_evidence_does_not_glue_204_217_different_defects():
    # оба трогают check_pr.py, но #217 не называет строку вовсе — пересечения
    # диапазонов нет, склейки быть не должно (класс #204/#217 назван прямо
    # в задаче #244 как "не путать")
    tasks = [
        task_issue(204, "review:large-ok не ставит автоматика", ISSUE_204_BODY),
        task_issue(217, "PR может молча откатить main", ISSUE_217_BODY),
    ]
    assert ri.check_duplicate_evidence(tasks) == []


def test_duplicate_evidence_ignores_markdown_files():
    # честный потолок: .md исключены (см. docstring extract_locators) — общее
    # правило AGENTS.md, процитированное двумя НЕсвязанными задачами, не
    # должно склеивать их
    body_a = "Смотри правило `AGENTS.md:77` про тормоз без газа."
    body_b = "То же правило `AGENTS.md:77` касается и этого случая."
    tasks = [task_issue(1, "A", body_a), task_issue(2, "B", body_b)]
    assert ri.check_duplicate_evidence(tasks) == []


def test_duplicate_evidence_mutation_guard():
    tasks = [
        task_issue(202, "A", ISSUE_202_BODY),
        task_issue(213, "B", ISSUE_213_BODY),
    ]
    assert len(ri.check_duplicate_evidence(tasks)) == 1
    # мутация: убрать сравнение файлов (сравнивать только диапазоны) слило бы
    # СОВЕРШЕННО разные файлы с совпадающими номерами строк — тест ниже
    # доказывает, что _locators_overlap требует совпадения файла
    assert ri._locators_overlap(("a.py", 10, 20), ("b.py", 10, 20)) is False
    assert ri._locators_overlap(("a.py", 10, 20), ("a.py", 15, 25)) is True


# ══════════════════════════════════════════════════════════════════════════
# Инвариант 6: защита main-ветки не откатилась молча (#341)
# ══════════════════════════════════════════════════════════════════════════

# Прод-форма — сырой `gh api repos/mytab0r/edge-harness/branches/main/protection`
# на момент задачи #341 (2026-09-06), не пересказ.
HEALTHY_PROTECTION = {
    "required_status_checks": {"strict": True, "contexts": ["test", "contract"]},
    "enforce_admins": {"enabled": True},
    "allow_force_pushes": {"enabled": False},
    "allow_deletions": {"enabled": False},
}


def test_branch_protection_healthy_snapshot_no_violations():
    assert ri.check_branch_protection_drift(HEALTHY_PROTECTION) == []


def test_branch_protection_flags_enforce_admins_disabled():
    # Живой класс задачи #341: enforce_admins стоял в false, admin-токен
    # сливал мимо всех обязательных проверок (HTTP 405 при попытке
    # воспроизвести проверку задним числом подтвердил пропуск).
    broken = {**HEALTHY_PROTECTION, "enforce_admins": {"enabled": False}}
    violations = ri.check_branch_protection_drift(broken)
    assert len(violations) == 1
    assert violations[0]["setting"] == "enforce_admins"
    assert "проверок" in violations[0]["consequence"]


def test_branch_protection_flags_missing_context():
    broken = {**HEALTHY_PROTECTION,
              "required_status_checks": {"strict": True, "contexts": ["test"]}}
    violations = ri.check_branch_protection_drift(broken)
    assert len(violations) == 1
    assert violations[0]["setting"] == "required_status_checks.contexts"
    assert violations[0]["actual"] == ["test"]
    assert violations[0]["expected"] == sorted(ri.EXPECTED_STATUS_CHECK_CONTEXTS)


def test_branch_protection_flags_extra_context_too():
    # Не только пропажа контекста — лишний неожиданный контекст тоже дрейф
    # (кто-то включил обязательную проверку, для которой не подтверждён
    # живой прогон, и вся очередь PR рискует зависнуть в «Expected»).
    broken = {**HEALTHY_PROTECTION,
              "required_status_checks": {"strict": True,
                                          "contexts": ["test", "contract", "harness/review"]}}
    violations = ri.check_branch_protection_drift(broken)
    assert len(violations) == 1
    assert violations[0]["setting"] == "required_status_checks.contexts"


def test_branch_protection_flags_strict_disabled():
    broken = {**HEALTHY_PROTECTION,
              "required_status_checks": {"strict": False, "contexts": ["test", "contract"]}}
    violations = ri.check_branch_protection_drift(broken)
    assert len(violations) == 1
    assert violations[0]["setting"] == "required_status_checks.strict"


def test_branch_protection_flags_force_pushes_enabled():
    broken = {**HEALTHY_PROTECTION, "allow_force_pushes": {"enabled": True}}
    violations = ri.check_branch_protection_drift(broken)
    assert len(violations) == 1
    assert violations[0]["setting"] == "allow_force_pushes"


def test_branch_protection_flags_deletions_enabled():
    broken = {**HEALTHY_PROTECTION, "allow_deletions": {"enabled": True}}
    violations = ri.check_branch_protection_drift(broken)
    assert len(violations) == 1
    assert violations[0]["setting"] == "allow_deletions"


def test_branch_protection_missing_keys_treated_as_drift():
    # Ответ GitHub без ключа вовсе (сеть отдала урезанный объект, старый
    # формат) — трактуем как расхождение, не как «всё ок»: при сомнении гейт
    # не ослабляется (AGENTS.md).
    violations = ri.check_branch_protection_drift({})
    settings = {v["setting"] for v in violations}
    assert settings == {
        "enforce_admins", "required_status_checks.strict",
        "required_status_checks.contexts", "allow_force_pushes", "allow_deletions",
    }


def test_branch_protection_all_violations_name_a_consequence():
    # AGENTS.md: «Инвариант обязан называть, ЧТО сломается при расхождении».
    violations = ri.check_branch_protection_drift({})
    assert all(v["consequence"] for v in violations)


def test_branch_protection_opt_in_disabled_by_default(monkeypatch):
    # build_report() по умолчанию НЕ дёргает fetch_branch_protection вовсе:
    # GITHUB_TOKEN не может получить право administration ни при какой
    # правке permissions в workflow — вызов оттуда всегда 403. Гвардия ловит
    # регресс «кто-то включил инвариант 6 в стандартный отчёт по умолчанию»
    # мутацией — если бы default стал True, fetch полетел бы в FakeGh без
    # маршрута и тест упал бы AssertionError из самого FakeGh.
    fake = FakeGh({
        f"issues?state=open&labels={ri.TASK_LABEL}": [],
        "pulls?state=closed": [],
        "pulls?state=open": [],
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(ri, "OPENSPEC_CHANGES", Path("/nonexistent-openspec-changes"))
    now = utc(2026, 9, 6, 12, 0)
    lines, findings = ri.build_report("mytab0r/edge-harness", now)
    assert 6 not in findings
    assert any("⏭️" in line and "[6]" in line for line in lines)


def test_branch_protection_opt_in_enabled_reads_and_reports(monkeypatch):
    fake = FakeGh({
        f"issues?state=open&labels={ri.TASK_LABEL}": [],
        "pulls?state=closed": [],
        "pulls?state=open": [],
        "branches/main/protection": HEALTHY_PROTECTION,
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(ri, "OPENSPEC_CHANGES", Path("/nonexistent-openspec-changes"))
    now = utc(2026, 9, 6, 12, 0)
    lines, findings = ri.build_report("mytab0r/edge-harness", now, check_branch_protection=True)
    assert findings[6] == []
    assert any("💚" in line and "[6]" in line for line in lines)


def test_branch_protection_not_in_ci_gating():
    # Не может быть в CI_GATING по конструкции: включение обязательной
    # проверки для инварианта, которому GITHUB_TOKEN не может дать ответ,
    # красило бы main на КАЖДОМ прогоне — хуже отсутствия проверки.
    assert 6 not in ri.CI_GATING


# ══════════════════════════════════════════════════════════════════════════
# Холостой ход: здоровый снимок — 0 нарушений, 0 мутирующих вызовов
# ══════════════════════════════════════════════════════════════════════════


def test_idle_guard_healthy_snapshot_no_violations_no_mutating_calls(tmp_path, monkeypatch):
    """Мутация-доказательство холостого хода: здоровое состояние во ВСЕХ пяти
    инвариантах разом не должно вызвать ни одного -X POST/PUT/DELETE."""
    healthy_tasks = [task_issue(1, "здоровая задача", "нет ссылок", assignees=("someone",))]
    healthy_open_pulls = [open_pr(50, "#1", labels=["review:ok", "ai:ok"])]
    healthy_merged = []  # нет слитых PR вовсе — задача 1 занята, не free

    fake = FakeGh({
        f"issues?state=open&labels={ri.TASK_LABEL}": healthy_tasks,
        "pulls?state=closed": [],
        "pulls?state=open": healthy_open_pulls,
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(ri, "OPENSPEC_CHANGES", tmp_path / "changes-empty")

    now = utc(2026, 9, 3, 12, 0)
    lines, findings = ri.build_report("mytab0r/edge-harness", now)

    assert all(not v for v in findings.values()), f"здоровый снимок не должен давать нарушений: {findings}"
    assert fake.mutating_calls() == [], "чтение состояния не должно ничего менять"
    assert all("🚨" not in line for line in lines)

    # --orchestra тоже не должен мутировать на здоровом снимке: без нарушений
    # run_escalations не обязан звать escalate() (никаких POST в WATCHDOG_ISSUE
    # и Telegram).
    escalation_lines = ri.run_escalations("mytab0r/edge-harness", findings)
    assert escalation_lines == []
    assert fake.mutating_calls() == []
