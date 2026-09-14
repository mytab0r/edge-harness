#!/usr/bin/env python3
"""Тесты review_findings.py — реестр незакрытых находок ревью, ключ файл
(#1262, объединяет #1217).

Запуск: python -m pytest scripts/lib/test_review_findings.py -q
"""

import importlib.util
import json
from pathlib import Path

_DIR = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("review_findings", _DIR / "review_findings.py")
rf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rf)  # type: ignore[union-attr]


REPO = "mytab0r/edge-harness"


class FakeGh:
    """Тот же приём, что FakeGh в test_scheduler.py: маршрутизация по
    подстроке пути, каждый вызов записан — доказательство «сколько сетевых
    вызовов и каких» для тестов на холостой ход/минимальность записи."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, *args):
        joined = " ".join(args)
        self.calls.append(joined)
        for fragment, result in self.routes.items():
            if fragment in joined:
                if isinstance(result, Exception):
                    raise result
                return result
        raise AssertionError(f"нет маршрута для: {joined}")


def not_found(path: str) -> RuntimeError:
    return RuntimeError(f"gh api {path}: HTTP 404: Not Found (https://api.github.com/...)")


def encode_registry(registry: dict) -> dict:
    import base64
    text = rf.dump_registry(registry)
    return {"content": base64.b64encode(text.encode("utf-8")).decode("ascii"), "sha": "blobsha1"}


# ── формат реестра: load/dump/add/close/lookup ────────────────────────────────

def test_empty_registry_shape():
    assert rf.empty_registry() == {"next_id": 1, "findings": []}


def test_load_registry_empty_text_is_empty_registry():
    assert rf.load_registry("") == rf.empty_registry()
    assert rf.load_registry(None) == rf.empty_registry()


def test_add_finding_assigns_sequential_ids():
    registry = rf.empty_registry()
    first = rf.add_finding(registry, "scripts/a.py", "Заголовок 1", "детали", 100, "2026-09-14T00:00:00Z")
    second = rf.add_finding(registry, "scripts/b.py", "Заголовок 2", "", 100, "2026-09-14T00:00:00Z")
    assert (first, second) == (1, 2)
    assert registry["next_id"] == 3
    assert [f["status"] for f in registry["findings"]] == ["open", "open"]


def test_close_findings_marks_status_and_returns_actually_closed():
    registry = rf.empty_registry()
    rf.add_finding(registry, "scripts/a.py", "Раз", "", 100, "t")
    rf.add_finding(registry, "scripts/b.py", "Два", "", 100, "t")
    closed = rf.close_findings(registry, [1, 999], closed_by_pr=200)
    assert closed == [1]  # 999 не существует — не ошибка, просто не в ответе
    statuses = {f["id"]: f["status"] for f in registry["findings"]}
    assert statuses == {1: "closed", 2: "open"}
    assert next(f for f in registry["findings"] if f["id"] == 1)["closed_by_pr"] == 200


def test_close_findings_already_closed_id_not_reclosed_twice():
    registry = rf.empty_registry()
    rf.add_finding(registry, "scripts/a.py", "Раз", "", 100, "t")
    rf.close_findings(registry, [1], closed_by_pr=200)
    # Повторное закрытие того же id другим PR — не переписывает closed_by_pr
    # (id уже не в open_ids к моменту второго вызова).
    second = rf.close_findings(registry, [1], closed_by_pr=300)
    assert second == []
    assert next(f for f in registry["findings"] if f["id"] == 1)["closed_by_pr"] == 200


def test_open_findings_for_files_filters_by_file_and_status():
    registry = rf.empty_registry()
    rf.add_finding(registry, "scripts/a.py", "Раз", "", 100, "t")
    rf.add_finding(registry, "scripts/b.py", "Два", "", 100, "t")
    rf.close_findings(registry, [1], closed_by_pr=200)
    found = rf.open_findings_for_files(registry, ["scripts/a.py", "scripts/b.py", "scripts/c.py"])
    assert [f["title"] for f in found] == ["Два"]


# ── render_findings_section / render_unavailable — текст промпта ────────────

def test_render_findings_section_empty_says_no_findings_explicitly():
    text = rf.render_findings_section([])
    assert "нет" in text.lower()


def test_render_findings_section_groups_by_file_and_carries_id():
    findings = [
        {"id": 5, "file": "a.py", "title": "Т1", "detail": "д1", "source_pr": 10},
        {"id": 6, "file": "a.py", "title": "Т2", "detail": "", "source_pr": 11},
    ]
    text = rf.render_findings_section(findings)
    assert "### a.py" in text
    assert "[5]" in text and "[6]" in text
    assert "Т1" in text and "Т2" in text
    assert "источник PR #10" in text


def test_render_unavailable_names_the_reason():
    text = rf.render_unavailable("HTTP 500")
    assert "HTTP 500" in text
    assert "означает" in text  # не путает "недоступно" с "находок нет"


# ── fetch_registry: контент-API, 404 -> пустой реестр без ошибки ────────────

def test_fetch_registry_missing_branch_returns_empty_with_no_sha():
    fake = FakeGh({f"repos/{REPO}/contents/findings.json": not_found("contents/findings.json")})
    registry, sha = rf.fetch_registry(fake, REPO)
    assert registry == rf.empty_registry()
    assert sha is None


def test_fetch_registry_decodes_existing_content():
    stored = rf.empty_registry()
    rf.add_finding(stored, "a.py", "Т", "", 1, "t")
    fake = FakeGh({f"repos/{REPO}/contents/findings.json": encode_registry(stored)})
    registry, sha = rf.fetch_registry(fake, REPO)
    assert registry == stored
    assert sha == "blobsha1"


def test_fetch_registry_reraises_non_404_errors():
    fake = FakeGh({
        f"repos/{REPO}/contents/findings.json": RuntimeError("gh api ...: HTTP 500: Internal Server Error"),
    })
    try:
        rf.fetch_registry(fake, REPO)
        assert False, "ожидался RuntimeError"
    except RuntimeError as error:
        assert "HTTP 500" in str(error)


# ── sync_after_merge: единственная точка мутации ────────────────────────────

def test_sync_after_merge_adds_only_items_with_file():
    # Порядок маршрутов важен (тот же класс, что FakeGh в test_scheduler.py,
    # см. её докстринг): "-X PUT .../contents/findings.json" и "-X POST
    # .../git/refs" — ПОДСТРОКИ более общих GET-фрагментов ниже, обязаны
    # матчиться ПЕРВЫМИ, иначе PUT/POST молча подхватили бы 404-заглушку GET.
    fake = FakeGh({
        f"-X POST repos/{REPO}/git/refs": {"ref": "refs/heads/data/review-findings"},
        f"-X PUT repos/{REPO}/contents/findings.json": {"content": {"sha": "newsha"}},
        f"repos/{REPO}/contents/findings.json": not_found("contents/findings.json"),
        f"repos/{REPO}/git/ref/heads/data/review-findings": not_found("git/ref/heads/data/review-findings"),
        f"repos/{REPO}/git/ref/heads/main": {"object": {"sha": "mainsha"}},
    })
    items = [
        {"title": "С файлом", "file": "scripts/a.py", "detail": "деталь"},
        {"title": "Без файла", "file": None, "detail": ""},
        {"title": "Пустая строка файла", "file": "  ", "detail": ""},
    ]
    result = rf.sync_after_merge(fake, REPO, 163, items, [], "2026-09-14T00:00:00Z")
    assert result == {"added": [1], "closed": [], "skipped": 2}
    put_calls = [c for c in fake.calls if c.startswith("-X PUT")]
    assert len(put_calls) == 1
    # Ветка создана ДО записи (sha реестра был None).
    assert any(c.startswith("-X POST") and "git/refs" in c for c in fake.calls)


def test_sync_after_merge_creates_branch_only_when_missing():
    stored = rf.empty_registry()
    fake = FakeGh({
        f"-X PUT repos/{REPO}/contents/findings.json": {"content": {"sha": "newsha"}},
        f"repos/{REPO}/contents/findings.json": encode_registry(stored),
    })
    items = [{"title": "Т", "file": "a.py", "detail": ""}]
    rf.sync_after_merge(fake, REPO, 163, items, [], "t")
    assert not any("git/refs" in c for c in fake.calls)  # ветка уже была (sha не None)


def test_sync_after_merge_closes_resolved_ids():
    stored = rf.empty_registry()
    rf.add_finding(stored, "a.py", "Старая находка", "", 100, "t")
    fake = FakeGh({
        f"-X PUT repos/{REPO}/contents/findings.json": {"content": {"sha": "newsha"}},
        f"repos/{REPO}/contents/findings.json": encode_registry(stored),
    })
    result = rf.sync_after_merge(fake, REPO, 200, [], [1], "t")
    assert result == {"added": [], "closed": [1], "skipped": 0}


def test_sync_after_merge_noop_makes_no_write_call():
    # Мутация-гвардия: если убрать проверку "not added_ids and not closed_ids"
    # (вернуть безусловный PUT), этот тест красит AssertionError фейковой
    # заглушки — маршрута для PUT здесь нет специально.
    stored = rf.empty_registry()
    fake = FakeGh({f"repos/{REPO}/contents/findings.json": encode_registry(stored)})
    result = rf.sync_after_merge(fake, REPO, 163, [{"title": "Т", "file": None, "detail": ""}], [], "t")
    assert result == {"added": [], "closed": [], "skipped": 1}
    assert not any(c.startswith("-X PUT") for c in fake.calls)


# ── parse_resolved_ids: НАХОДКА-ЗАКРЫТА: <id> ────────────────────────────────

def test_parse_resolved_ids_extracts_ids_in_order_without_duplicates():
    answer = (
        "Проза.\n"
        "НАХОДКА-ЗАКРЫТА: 5\n"
        "НАХОДКА-ЗАКРЫТА: #12\n"
        "НАХОДКА-ЗАКРЫТА: 5\n"
        "ВЕРДИКТ: approve"
    )
    assert rf.parse_resolved_ids(answer) == [5, 12]


def test_parse_resolved_ids_empty_answer_returns_empty_list():
    assert rf.parse_resolved_ids("") == []
    assert rf.parse_resolved_ids("ВЕРДИКТ: approve") == []


# ── resolved-findings маркер в теле PR (без сети для after_merge) ───────────

def test_parse_resolved_marker_absent_is_empty_list():
    assert rf.parse_resolved_marker("Обычное описание PR.") == []
    assert rf.parse_resolved_marker("") == []


def test_merge_resolved_marker_creates_new_marker():
    new_body = rf.merge_resolved_marker("Описание PR.", [7])
    assert new_body is not None
    assert "Описание PR." in new_body
    assert rf.parse_resolved_marker(new_body) == [7]


def test_merge_resolved_marker_unions_with_existing_ids():
    body = "Описание.\n\n<!-- ai-review:resolved-findings:3 -->\n"
    new_body = rf.merge_resolved_marker(body, [7])
    assert new_body is not None
    assert rf.parse_resolved_marker(new_body) == [3, 7]


def test_merge_resolved_marker_noop_when_all_ids_already_present():
    body = "Описание.\n\n<!-- ai-review:resolved-findings:3,7 -->\n"
    assert rf.merge_resolved_marker(body, [7]) is None
    assert rf.merge_resolved_marker(body, []) is None
