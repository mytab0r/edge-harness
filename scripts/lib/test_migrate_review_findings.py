#!/usr/bin/env python3
"""Тесты migrate_review_findings.py — миграция 110 задач-хвостов в реестр
находок (#1262, этап 2/3). Фикстуры — реальные тела живых хвостов
(`fixtures_tail_1265.json`/`fixtures_tail_1258.json`/`fixtures_tail_1252.json`,
сняты `gh api repos/mytab0r/edge-harness/issues/<N>` 2026-09-14), не пересказ
формата (AGENTS.md, «тест кормит прод-форму данных, а не пересказ»).

Запуск: python -m pytest scripts/lib/test_migrate_review_findings.py -q
"""

import importlib.util
import json
from pathlib import Path

_DIR = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("migrate_review_findings", _DIR / "migrate_review_findings.py")
mrf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mrf)  # type: ignore[union-attr]


def _fixture(number: int) -> dict:
    data = json.loads((_DIR / f"fixtures_tail_{number}.json").read_text(encoding="utf-8"))
    return {"number": data["number"], "title": data["title"], "body": data["body"]}


# ── extract_findings / pr_number_from_title — реальные тела ──────────────────

def test_extract_findings_on_real_tail_1265_gets_all_three_bullets():
    tail = _fixture(1265)
    findings = mrf.extract_findings(tail["body"])
    assert len(findings) == 3
    assert findings[0].startswith("окно истории уже заявленного")
    assert findings[2].startswith("`_RETIRED_DEFECT_CLASS_TRACKER_ISSUE`")


def test_pr_number_from_title_on_real_tails():
    assert mrf.pr_number_from_title(_fixture(1265)["title"]) == 1259
    assert mrf.pr_number_from_title(_fixture(1258)["title"]) == 1209
    assert mrf.pr_number_from_title(_fixture(1252)["title"]) == 1244


def test_pr_number_from_title_rejects_unrelated_title():
    assert mrf.pr_number_from_title("Что-то другое") is None


# ── guess_exact_file — токен пути в тексте находки, реально в дереве ─────────

def test_guess_exact_file_finds_full_path_with_line_number():
    text = "живые тексты в этом же PR показывают (ai_review.py:137)"
    repo_files = {"scripts/review/ai_review.py", "scripts/review/check_pr.py"}
    assert mrf.guess_exact_file(text, repo_files) == "scripts/review/ai_review.py"


def test_guess_exact_file_unique_tail_match():
    text = "в PATCHES.md о ней ни слова"
    repo_files = {"dsh-edge/PATCHES.md", "scripts/review/ai_review.py"}
    assert mrf.guess_exact_file(text, repo_files) == "dsh-edge/PATCHES.md"


def test_guess_exact_file_ambiguous_tail_match_returns_none():
    # Два файла заканчиваются на "config.py" — неоднозначно, не угадываем.
    text = "правка config.py ломает тест"
    repo_files = {"a/config.py", "b/config.py"}
    assert mrf.guess_exact_file(text, repo_files) is None


def test_guess_exact_file_no_match_returns_none():
    text = "Прозаическая находка вообще без файла."
    assert mrf.guess_exact_file(text, {"scripts/review/ai_review.py"}) is None


# ── plan_migration: сквозной прогон на реальных фикстурах ────────────────────

def test_plan_migration_on_real_tails_classifies_every_finding():
    tails = [_fixture(1265), _fixture(1258), _fixture(1252)]
    repo_files = {
        "scripts/review/ai_review.py", "dsh-edge/PATCHES.md",
        "scripts/review/defect_classes.py",
    }
    pr_files = {1259: ["scripts/orchestra/scheduler.py"],
                1209: ["dsh-edge/PATCHES.md"],
                1244: ["scripts/review/ai_review.py", "scripts/review/defect_classes.py"]}
    plan, stats = plan_migration_with_lookup(tails, repo_files, pr_files)

    total_expected = sum(len(mrf.extract_findings(t["body"])) for t in tails)
    assert stats["total_findings"] == total_expected
    # Ноль потерянных: каждая находка попадает в план ровно одним из трёх
    # методов (exact/broadcast/manual) — считается, не тонет молча.
    assert stats["exact"] + stats["broadcast"] + stats["manual"] == total_expected
    # ai_review.py:137 из хвоста #1252 — точный файл.
    exact_titles = [p["title"] for p in plan if p["method"] == "exact"]
    assert any("markdown-обрамление" in t for t in exact_titles)


def plan_migration_with_lookup(tails, repo_files, pr_files_map):
    def lookup(pr_number):
        return pr_files_map.get(pr_number)
    return mrf.plan_migration(tails, repo_files, lookup)


def test_plan_migration_manual_when_pr_unavailable():
    tails = [{"number": 999, "title": "Хвост чеклиста ревью PR #500",
             "body": "- [ ] Находка без опознаваемого файла и с недоступным PR"}]
    plan, stats = mrf.plan_migration(tails, set(), lambda pr: None)
    assert stats == {"exact": 0, "broadcast": 0, "manual": 1, "total_findings": 1, "tails": 1}
    assert plan == [{"tail_issue": 999, "source_pr": 500, "file": None,
                     "title": "Находка без опознаваемого файла и с недоступным PR",
                     "detail": "", "method": "manual"}]


def test_plan_migration_broadcast_when_no_exact_file_but_pr_available():
    tails = [{"number": 999, "title": "Хвост чеклиста ревью PR #500",
             "body": "- [ ] Находка про общую архитектуру, файл не назван"}]
    plan, stats = mrf.plan_migration(tails, set(), lambda pr: ["a.py", "b.py"])
    assert stats["broadcast"] == 1
    assert stats["exact"] == 0
    assert {p["file"] for p in plan} == {"a.py", "b.py"}
    assert all(p["method"] == "broadcast" for p in plan)


def test_plan_migration_caps_broadcast_fanout_to_manual(monkeypatch):
    # Замер живого прогона 2026-09-14: PR #409 тронул 100 файлов — broadcast
    # на ВСЕ дал бы 100 записей одной находки. Свыше MAX_BROADCAST_FANOUT —
    # ручной разбор, не частичный/полный broadcast.
    monkeypatch.setattr(mrf, "MAX_BROADCAST_FANOUT", 3)
    tails = [{"number": 999, "title": "Хвост чеклиста ревью PR #409",
             "body": "- [ ] Находка на PR с большим диффом"}]
    huge_pr_files = [f"file{i}.py" for i in range(100)]
    plan, stats = mrf.plan_migration(tails, set(), lambda pr: huge_pr_files)
    assert stats["broadcast"] == 0
    assert stats["manual"] == 1
    assert plan == [{"tail_issue": 999, "source_pr": 409, "file": None,
                     "title": "Находка на PR с большим диффом", "detail": "", "method": "manual"}]


def test_plan_migration_zero_findings_lost_across_all_three_real_fixtures():
    # Явная проверка требования «ноль потерянных» (не пожелание) на трёх
    # реальных хвостах: каждая извлечённая находка учтена РОВНО один раз.
    tails = [_fixture(1265), _fixture(1258), _fixture(1252)]
    plan, stats = mrf.plan_migration(tails, set(), lambda pr: None)
    assert len(plan) == stats["total_findings"]
    assert stats["manual"] == stats["total_findings"]  # без repo_files/pr_files всё уходит в manual
    assert all(item["file"] is None for item in plan)
