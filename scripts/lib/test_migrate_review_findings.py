#!/usr/bin/env python3
"""Тесты migrate_review_findings.py — миграция 110 задач-хвостов в реестр
находок (#1262, этап 2/3). Фикстуры — реальные тела живых хвостов
(`fixtures_tail_1265.json`/`fixtures_tail_1258.json`/`fixtures_tail_1252.json`,
сняты `gh api repos/mytab0r/edge-harness/issues/<N>` 2026-09-14), не пересказ
формата (AGENTS.md, «тест кормит прод-форму данных, а не пересказ»).

Запуск: python -m pytest scripts/lib/test_migrate_review_findings.py -q
"""

import base64
import importlib.util
import json
from pathlib import Path

_DIR = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("migrate_review_findings", _DIR / "migrate_review_findings.py")
mrf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mrf)  # type: ignore[union-attr]

_rf_spec = importlib.util.spec_from_file_location("review_findings_for_migrate_tests", _DIR / "review_findings.py")
rf = importlib.util.module_from_spec(_rf_spec)
_rf_spec.loader.exec_module(rf)  # type: ignore[union-attr]


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


# ── fetch_pr_files: удалённые файлы не годятся для broadcast ────────────────

def test_fetch_pr_files_skips_removed_files(monkeypatch):
    # files API отдаёт удалённым файлам status: "removed" — broadcast на
    # такой путь замораживает находку навсегда (путь не появится в будущем
    # диффе, пока его не воссоздадут; design.md развилка 5 отвергла
    # `_legacy/pr-<N>` ровно за это). Переименованные/добавленные/правленные
    # остаются — их будущие диффы реальны (ревью PR #1268).
    def fake_gh(*args):
        assert "pulls/1/files" in " ".join(args)
        return [
            {"filename": "live.py", "status": "added"},
            {"filename": "gone.py", "status": "removed"},
            {"filename": "renamed-to.py", "status": "renamed"},
            {"filename": "edited.py", "status": "modified"},
        ]

    monkeypatch.setattr(mrf, "gh", fake_gh)
    assert mrf.fetch_pr_files("mytab0r/edge-harness", 1) == [
        "live.py", "renamed-to.py", "edited.py"]


def test_plan_migration_manual_when_every_pr_file_removed(monkeypatch):
    # PR, удаливший ВСЕ свои файлы, не даёт broadcast ни одного живого пути:
    # после фильтра removed список пуст, а пустой список трактуется как
    # «broadcast невозможен» — ручной разбор, не молчаливая «миграция» в
    # мёртвые пути (та же ветка, что PR недоступен).
    monkeypatch.setattr(
        mrf, "fetch_pr_files", lambda repo, pr: [])  # уже отфильтровано выше
    tails = [{"number": 999, "title": "Хвост чеклиста ревью PR #500",
              "body": "- [ ] Находка на PR, удалившем единственный файл"}]
    plan, stats = mrf.plan_migration(
        tails, set(), lambda pr: mrf.fetch_pr_files("mytab0r/edge-harness", pr))
    assert stats["broadcast"] == 0
    assert stats["manual"] == 1
    assert plan[0]["file"] is None


# ── cmd_apply: идемпотентность повторного прогона ───────────────────────────

def _make_server(stored) -> dict:
    """Состояние эмулированного Contents API: то, что GET отдаёт до первого
    PUT. ЕДИН на весь сценарий (включая повторный прогон cmd_apply) — иначе
    второй прогон увидел бы пустой реестр и задвоение не поймалось бы."""
    return {"content": base64.b64encode(rf.dump_registry(stored).encode("utf-8")).decode("ascii")}


def _apply_plan(monkeypatch, tmp_path, server, plan_items):
    """Прогон cmd_apply с эмуляцией сервера: GET всегда отдаёт актуальное
    содержимое, PUT его перезаписывает. cmd_apply мутирует СВОЙ экземпляр
    реестра (результат fetch_registry, распарсенный из текста), поэтому
    наблюдаемый эффект — ТОЛЬКО содержимое PUT. Возвращает
    (rc, число PUT, сервер)."""
    import argparse
    puts = []

    def fake_gh(*args):
        joined = " ".join(args)
        if joined.startswith("-X PUT repos/mytab0r/edge-harness/contents/findings.json"):
            for arg in args:
                if arg.startswith("content="):
                    server["content"] = arg[len("content="):]
            puts.append(joined)
            return {"content": {"sha": "newsha"}}
        return {"content": server["content"], "sha": "blobsha"}

    monkeypatch.setattr(mrf, "gh", fake_gh)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"plan": plan_items, "stats": {}}), encoding="utf-8")
    rc = mrf.cmd_apply(argparse.Namespace(repo="mytab0r/edge-harness", plan=str(plan_path)))
    return rc, len(puts), server


def _findings_on_server(server) -> list[dict]:
    text = base64.b64decode(server["content"]).decode("utf-8")
    return json.loads(text)["findings"]


def test_cmd_apply_writes_findings_on_first_run(monkeypatch, tmp_path):
    stored = rf.empty_registry()
    plan = [
        {"tail_issue": 900, "source_pr": 800, "file": "scripts/a.py",
         "title": "Находка раз", "detail": "", "method": "exact"},
        {"tail_issue": 900, "source_pr": 800, "file": "scripts/b.py",
         "title": "Находка два", "detail": "", "method": "broadcast"},
        {"tail_issue": 900, "source_pr": 800, "file": None,
         "title": "Ручной разбор", "detail": "", "method": "manual"},
    ]
    server = _make_server(stored)
    rc, puts, server = _apply_plan(monkeypatch, tmp_path, server, plan)
    assert rc == 0
    assert puts == 1
    findings = _findings_on_server(server)
    assert len(findings) == 2  # manual (file=None) не ключуется
    assert {f["file"] for f in findings} == {"scripts/a.py", "scripts/b.py"}


def test_cmd_apply_repeat_run_is_noop_not_duplicate(monkeypatch, tmp_path):
    # Ретрай после сбоя / «а записалось ли?» не задваивает: тройка
    # (file, title, source_pr) уже в реестре — пропуск, записи нет
    # (ревью PR #1268, чеклист тела).
    stored = rf.empty_registry()
    plan = [
        {"tail_issue": 900, "source_pr": 800, "file": "scripts/a.py",
         "title": "Находка раз", "detail": "", "method": "exact"},
        {"tail_issue": 900, "source_pr": 800, "file": "scripts/b.py",
         "title": "Находка два", "detail": "", "method": "broadcast"},
    ]
    server = _make_server(stored)
    rc1, puts1, server = _apply_plan(monkeypatch, tmp_path, server, plan)
    assert (rc1, puts1) == (0, 1)
    assert len(_findings_on_server(server)) == 2

    rc2, puts2, server = _apply_plan(monkeypatch, tmp_path, server, plan)
    assert rc2 == 0
    assert puts2 == 0  # повторный прогон БЕЗ записи
    assert len(_findings_on_server(server)) == 2  # и без дублей


def test_cmd_apply_dedupes_duplicates_inside_single_plan(monkeypatch, tmp_path):
    # Два одинаковых пункта в одном плане (два хвоста одного PR) — одна
    # запись, не две.
    stored = rf.empty_registry()
    plan = [
        {"tail_issue": 900, "source_pr": 800, "file": "scripts/a.py",
         "title": "Находка раз", "detail": "", "method": "exact"},
        {"tail_issue": 901, "source_pr": 800, "file": "scripts/a.py",
         "title": "Находка раз", "detail": "", "method": "exact"},
    ]
    server = _make_server(stored)
    rc, puts, server = _apply_plan(monkeypatch, tmp_path, server, plan)
    assert rc == 0
    assert puts == 1
    assert len(_findings_on_server(server)) == 1
