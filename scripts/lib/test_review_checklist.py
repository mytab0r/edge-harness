#!/usr/bin/env python3
"""Тесты review_checklist.py — третья категория находок ревью (#462): блок
ЗАМЕЧАНИЕ, слияние с чеклистом тела PR, незакрытые пункты при слиянии.

Запуск: python -m pytest scripts/lib/test_review_checklist.py -q
"""

import importlib.util
from pathlib import Path

_DIR = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("review_checklist", _DIR / "review_checklist.py")
rc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rc)  # type: ignore[union-attr]


# ── parse_remarks: блок ЗАМЕЧАНИЕ / КОНЕЦ ЗАМЕЧАНИЯ ───────────────────────────

def test_parse_remarks_extracts_title_and_body():
    answer = (
        "Проза перед блоком.\n\n"
        "ЗАМЕЧАНИЕ: docs/foo.md — устаревшее число\n"
        "Порог в коде строгий, поправь формулировку.\n"
        "КОНЕЦ ЗАМЕЧАНИЯ\n\n"
        "ВЕРДИКТ: approve"
    )
    remarks = rc.parse_remarks(answer)
    assert remarks == [{"title": "docs/foo.md — устаревшее число",
                         "body": "Порог в коде строгий, поправь формулировку."}]


def test_parse_remarks_drops_unclosed_block():
    # Незакрытый блок отбрасывается целиком — тот же принцип, что parse_tasks
    # в ai_review.py: половина находки хуже отсутствия находки.
    answer = "ЗАМЕЧАНИЕ: Забытое\nТело без закрытия.\nВЕРДИКТ: approve"
    assert rc.parse_remarks(answer) == []


def test_parse_remarks_multiple_blocks():
    answer = (
        "ЗАМЕЧАНИЕ: Первое\nТело раз.\nКОНЕЦ ЗАМЕЧАНИЯ\n"
        "ЗАМЕЧАНИЕ: Второе\nТело два.\nКОНЕЦ ЗАМЕЧАНИЯ\n"
        "ВЕРДИКТ: approve"
    )
    remarks = rc.parse_remarks(answer)
    assert [r["title"] for r in remarks] == ["Первое", "Второе"]


# ── merge_checklist: слияние в тело PR, отмеченные пункты не затираются ──────

def test_merge_checklist_appends_new_section_when_none_exists():
    body = "Описание PR без чеклиста."
    remarks = [{"title": "Замечание раз", "body": "Поправь X."}]
    new_body = rc.merge_checklist(body, remarks)
    assert new_body is not None
    assert "Описание PR без чеклиста." in new_body
    assert rc.CHECKLIST_BEGIN in new_body and rc.CHECKLIST_END in new_body
    assert "- [ ] **Замечание раз** — Поправь X." in new_body


def test_merge_checklist_no_new_remarks_no_existing_section_is_noop():
    assert rc.merge_checklist("Просто описание.", []) is None


def test_merge_checklist_preserves_checked_items_across_rounds():
    # Автор отметил пункт нативным чекбоксом GitHub — следующий раунд ревью
    # с НОВЫМ замечанием не должен снимать отметку со старого.
    body = (
        "Описание.\n\n"
        f"{rc.CHECKLIST_BEGIN}\n{rc.CHECKLIST_TITLE}\n\n"
        "- [x] **Старое замечание** — уже сделано\n"
        f"{rc.CHECKLIST_END}\n"
    )
    new_remarks = [{"title": "Новое замечание", "body": "Сделай Y."}]
    new_body = rc.merge_checklist(body, new_remarks)
    assert new_body is not None
    assert "- [x] **Старое замечание** — уже сделано" in new_body   # МУТАЦИЯ: если merge
    # перезаписывает существующие пункты как неотмеченные — эта строка исчезнет
    # (доказано вручную: замена `items.append` на построение списка заново из
    # одних remarks красит именно эту строку).
    assert "- [ ] **Новое замечание** — Сделай Y." in new_body


def test_merge_checklist_dedupes_by_exact_title_keeps_existing_state():
    # Повторная находка с тем же заголовком в следующем раунде не плодит
    # вторую строку — и не сбрасывает уже стоящую отметку. Заголовок сам
    # содержит « — » — регрессия «дедуп по первому тире» схлопнула бы его
    # с описанием и никогда не совпала бы с новым remark["title"].
    body = (
        f"{rc.CHECKLIST_BEGIN}\n{rc.CHECKLIST_TITLE}\n\n"
        "- [x] **Дубль — с тире в заголовке** — было тело раньше\n"
        f"{rc.CHECKLIST_END}\n"
    )
    remarks = [{"title": "Дубль — с тире в заголовке",
                "body": "Новое тело, которое не должно попасть."}]
    new_body = rc.merge_checklist(body, remarks)
    # Ничего нового не добавлено (заголовок уже был) — merge_checklist вправе
    # вернуть None (нет изменений) или тело без второй строки; проверяем
    # инвариант количества строк с «Дубль», а не факт PATCH.
    if new_body is not None:
        assert new_body.count("Дубль") == 1
        assert "- [x] **Дубль — с тире в заголовке** — было тело раньше" in new_body


def test_merge_checklist_returns_none_when_all_remarks_already_present():
    body = (
        f"{rc.CHECKLIST_BEGIN}\n{rc.CHECKLIST_TITLE}\n\n"
        "- [ ] **Уже здесь**\n"
        f"{rc.CHECKLIST_END}\n"
    )
    remarks = [{"title": "Уже здесь", "body": ""}]
    assert rc.merge_checklist(body, remarks) is None


# ── unresolved_items: подсчёт незакрытых пунктов на момент слияния ──────────

def test_unresolved_items_returns_only_unchecked():
    body = (
        f"{rc.CHECKLIST_BEGIN}\n{rc.CHECKLIST_TITLE}\n\n"
        "- [ ] **Не сделано**\n"
        "- [x] **Сделано**\n"
        "- [X] **Тоже сделано заглавной X**\n"
        f"{rc.CHECKLIST_END}\n"
    )
    assert rc.unresolved_items(body) == ["Не сделано"]


def test_unresolved_items_empty_without_section():
    assert rc.unresolved_items("Обычное описание PR без чеклиста.") == []


def test_unresolved_items_empty_when_all_checked():
    body = (
        f"{rc.CHECKLIST_BEGIN}\n{rc.CHECKLIST_TITLE}\n\n"
        "- [x] **Всё сделано**\n"
        f"{rc.CHECKLIST_END}\n"
    )
    assert rc.unresolved_items(body) == []


# ── tail_issue_*: текст задачи-хвоста ────────────────────────────────────────

def test_tail_issue_title_is_stable_per_pr():
    assert rc.tail_issue_title(163) == rc.tail_issue_title(163)
    assert rc.tail_issue_title(163) != rc.tail_issue_title(164)


def test_tail_issue_body_lists_all_unresolved_and_links_pr():
    body = rc.tail_issue_body("o/r", 163, ["Пункт раз", "Пункт два"])
    assert "PR #163" in body
    assert "https://github.com/o/r/pull/163" in body
    assert "- [ ] Пункт раз" in body
    assert "- [ ] Пункт два" in body
