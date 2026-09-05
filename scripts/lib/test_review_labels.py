#!/usr/bin/env python3
"""Тесты review_labels.py — единственного места правды для гейта слияния и
для выборочного подтягивания веток (#252).

fixtures_open_pulls_252.json — прод-форма, не пересказ: реальный ответ
`gh api "repos/mytab0r/edge-harness/pulls?state=open&per_page=100"` этого же
репозитория, снятый 2026-09-03 (после удаления собственного PR #252, чтобы
не засорять снимок). Поля сужены до number/draft/labels через `--jq`
(значения — те же самые, что вернул API, без пересказа) — полные объекты
несут в title/body случайные упоминания моделей из соседних задач, которые
ложно бьют guard'а #153 (стейл-провайдер/модель), а тесту здесь нужны только
labels. Тест разбирает то, что система реально отдаёт, а не наше
представление о формате.

Запуск: python -m pytest scripts/lib/test_review_labels.py -q
"""

import importlib.util
import json
from pathlib import Path

_DIR = Path(__file__).resolve().parent
SCRIPT = _DIR / "review_labels.py"
spec = importlib.util.spec_from_file_location("review_labels", SCRIPT)
review_labels = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review_labels)  # type: ignore[union-attr]

FIXTURE = _DIR / "fixtures_open_pulls_252.json"


def _load_pulls() -> list[dict]:
    with open(FIXTURE, encoding="utf-8") as file:
        return json.load(file)


# ── Гвардия #252 на прод-форме: только близкие к слиянию или в конфликте ─────────
# Снимок содержал (2026-09-03): #162/#230/#231/#237 — метка conflict
# (подтягивать, может расшить); ни у одного открытого PR на момент снимка не
# было ОБОИХ вердиктов review:ok+ai:ok разом (см. отдельный синтетический
# тест ниже для этой ветки предиката) — #247/#248/#249/#253/#241/#181/#173 и
# другие стоят с review:large/ai:failed/ai:changes-requested/contract:failed
# без обоих зелёных (не подтягивать — дорогое AI-ревью и сброс вердикта без
# пользы для PR, которому рано сливаться).
EXPECTED_SHOULD_UPDATE = {162, 230, 231, 237}


def test_should_update_branch_matches_expected_on_real_open_pulls():
    pulls = _load_pulls()
    numbers = {pull["number"] for pull in pulls}
    # Гвардия свежести самого теста: если состав открытых PR в фикстуре
    # изменится (новый снимок), список ожиданий не должен молча протухнуть.
    assert numbers == {
        263, 262, 261, 260, 253, 249, 248, 247, 241, 237, 231, 230, 181, 173, 167, 162, 108,
    }

    should_update = {
        pull["number"] for pull in pulls
        if review_labels.should_update_branch(pull["labels"])
    }
    assert should_update == EXPECTED_SHOULD_UPDATE


def test_should_update_branch_true_only_for_conflict_or_both_verdicts():
    pulls = _load_pulls()
    for pull in pulls:
        names = {label["name"] for label in pull["labels"]}
        expected = ("conflict" in names) or (
            review_labels.REVIEW_OK in names and review_labels.AI_OK in names
        )
        actual = review_labels.should_update_branch(pull["labels"])
        assert actual == expected, f"#{pull['number']}: labels={sorted(names)}"


def test_should_update_branch_true_when_both_verdicts_green_without_conflict():
    # На снимке фикстуры такого PR нет (см. комментарий у EXPECTED_SHOULD_UPDATE) —
    # ветка предиката «близок к слиянию» проверяется синтетически, не прод-формой.
    labels = [{"name": "review:ok"}, {"name": "ai:ok"}]
    assert review_labels.should_update_branch(labels) is True


def test_should_update_branch_accepts_label_name_set_and_dict_list():
    # _names() поддерживает обе прод-формы: список dict'ов API и множество имён
    # (см. review_labels._names) — предикат обязан работать с обеими.
    dict_form = [{"name": "conflict"}]
    set_form = {"conflict"}
    assert review_labels.should_update_branch(dict_form) is True
    assert review_labels.should_update_branch(set_form) is True
    assert review_labels.should_update_branch([{"name": "review:ok"}]) is False
    assert review_labels.should_update_branch({"review:ok"}) is False


def test_should_update_branch_false_on_empty_labels():
    assert review_labels.should_update_branch([]) is False
    assert review_labels.should_update_branch(set()) is False


# ── Мутация гвардии: снять фильтр — тест обязан покраснеть ───────────────────────
# Доказательство держится руками в отчёте задачи #252 (правка should_update_branch
# на `return True` роняет test_should_update_branch_matches_expected_on_real_open_pulls
# и test_should_update_branch_true_only_for_conflict_or_both_verdicts), не тут:
# постоянная мутация в файле теста была бы сама по себе живым багом.


# ── Вердикт AI переживает подтягивание main без изменения диффа (#252) ───────
#
# fixtures_pr292_merge_diff.json — прод-форма: `gh api
# repos/mytab0r/edge-harness/compare/<merge-base>...<sha>` (сужено до
# filename/status/sha) для PR #292 ДО слияния main (голова — собственный
# коммит автора 659b518) и ПОСЛЕ (голова — merge-коммит 5432ce5,
# подтянувший main 2026-09-04). У PR #292 ровно два коммита: свой + один
# `Merge branch 'main'` без конфликтов — ровно тот сценарий, который чинит
# check_pr.py. Ответ идентичен побайтово (замерено `git diff --stat` между
# merge-base и головой на обеих точках, до и после слияния — 9 файлов, те
# же имена, те же blob-sha) — доказывает, что diff_fingerprint зависит
# только от содержимого патчей, а не от merge-коммита.
#
# fixtures_pr253_edit_diff.json — тот же приём для настоящей правки автора:
# PR #253 без единого merge-коммита (только собственные коммиты), rev1 —
# `gh api compare/<base>...4ad42edab9` (исходный пуш), rev2 — то же для
# следующего коммита `92f13f2863` (правка по ревью, убрала файл
# `openspec/specs/journal-tasks-hands.md` из диффа). Реальная правка меняет
# список файлов — отпечаток обязан отличаться.

FIXTURE_PR292 = _DIR / "fixtures_pr292_merge_diff.json"
FIXTURE_PR253 = _DIR / "fixtures_pr253_edit_diff.json"


def _load(fixture: Path, key: str) -> list[dict]:
    with open(fixture, encoding="utf-8") as file:
        return json.load(file)[key]


def test_diff_fingerprint_unchanged_across_clean_merge_from_main_pr292():
    before = _load(FIXTURE_PR292, "before_merge")
    after = _load(FIXTURE_PR292, "after_merge")
    assert review_labels.diff_fingerprint(before) == review_labels.diff_fingerprint(after)


def test_diff_fingerprint_changes_on_real_edit_pr253():
    rev1 = _load(FIXTURE_PR253, "rev1")
    rev2 = _load(FIXTURE_PR253, "rev2")
    assert review_labels.diff_fingerprint(rev1) != review_labels.diff_fingerprint(rev2)


def test_diff_fingerprint_order_independent():
    # Порядок страниц API не должен влиять — отпечаток сортирует список сам.
    files = _load(FIXTURE_PR292, "before_merge")
    assert review_labels.diff_fingerprint(files) == review_labels.diff_fingerprint(
        list(reversed(files)))


def test_diff_fingerprint_same_size_different_content_differs():
    # Класс, который явно назвала задача #252: разные правки одного размера
    # не должны случайно совпасть — blob-sha, не число строк, различает их.
    a = [{"filename": "f.py", "status": "modified", "sha": "aaa111"}]
    b = [{"filename": "f.py", "status": "modified", "sha": "bbb222"}]
    assert review_labels.diff_fingerprint(a) != review_labels.diff_fingerprint(b)


def test_diff_unchanged_requires_stored_fingerprint():
    # Нет сохранённого отпечатка (комментарий без diff:, старый формат) —
    # трактуется как «изменился»: сброс безопаснее ложного сохранения.
    assert review_labels.diff_unchanged(None, "abc") is False
    assert review_labels.diff_unchanged("", "abc") is False


def test_diff_unchanged_true_only_on_exact_match():
    assert review_labels.diff_unchanged("abc", "abc") is True
    assert review_labels.diff_unchanged("abc", "abd") is False


def test_header_facts_reads_diff_field():
    body = "pr: 292\nhead: 5432ce5\nreviewer: approve\ndiff: deadbeef\n\nпроза\n"
    assert review_labels.header_facts(body) == {
        "pr": "292", "head": "5432ce5", "reviewer": "approve", "diff": "deadbeef",
    }


def test_header_facts_diff_optional_backward_compat():
    # Старые комментарии (до этой правки) не несут diff: — шапка разбирается
    # как раньше, без KeyError и без выдумывания значения.
    body = "pr: 140\nhead: abc\nreviewer: rework\n\nпроза\n"
    facts = review_labels.header_facts(body)
    assert facts == {"pr": "140", "head": "abc", "reviewer": "rework"}
    assert "diff" not in facts


def test_latest_ai_comment_picks_last_reviewer_fact_paginated():
    # Две страницы, по 2 «факта-комментария» на страницу плюс шум без фактов —
    # latest_ai_comment обязан пролистать обе и вернуть последний по порядку
    # выдачи API (симулирует прод-форму file_tasks._pages/latest_review_comment).
    page1 = [
        {"body": "болтовня без фактов"},
        {"body": "pr: 1\nhead: a\nreviewer: rework\ndiff: fp-a\n"},
    ] + [{"body": f"шум {i}"} for i in range(98)]
    page2 = [{"body": "pr: 1\nhead: b\nreviewer: approve\ndiff: fp-b\n"}]

    calls: list[str] = []

    def fake_gh(url: str):
        calls.append(url)
        # Разбор точного параметра, не подстрока: "per_page=100" сам содержит
        # подстроку "page=1" — substring-матч тут же зациклил бы пагинацию
        # (тот самый класс, из-за которого file_tasks._pages бьёт постранично
        # через "&", а не строкой целиком).
        query = url.split("?", 1)[1] if "?" in url else ""
        params = dict(pair.split("=", 1) for pair in query.split("&") if "=" in pair)
        page = params.get("page")
        if page == "1":
            return page1
        if page == "2":
            return page2
        return []

    comment = review_labels.latest_ai_comment("o/r", 1, fake_gh)
    assert comment is not None
    facts = review_labels.header_facts(comment["body"])
    assert facts == {"pr": "1", "head": "b", "reviewer": "approve", "diff": "fp-b"}
    assert any("page=1" in call for call in calls) and any("page=2" in call for call in calls)


def test_latest_ai_comment_none_when_no_fact_comments():
    assert review_labels.latest_ai_comment(
        "o/r", 1, lambda url: [{"body": "просто обсуждение, без шапки"}]) is None
