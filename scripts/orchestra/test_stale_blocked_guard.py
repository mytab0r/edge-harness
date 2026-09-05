#!/usr/bin/env python3
"""Тесты гвардии «протухшей» метки blocked (scripts/orchestra/stale_blocked_guard.py, #334).

Кормятся прод-формой:
- тело и эскалационный комментарий issue #268 сняты живым
  `gh issue view 268 --json body,comments` 2026-09-05 — на тот момент #268
  несёт метку `blocked` и в комментарии буквально называет причину маркером
  «Блокирована: #265»; #265 закрыт 2026-09-05T13:33:09Z
  (`gh issue view 265 --json state,closedAt`). Это случай, который гвардия
  обязана поймать.
- тело issue #216 (тем же способом, 2026-09-05) — тоже несёт метку `blocked`,
  но называет ТРИ номера союзом «И» без маркера («PR #162 … #163 … #164»);
  #163/#164 закрыты, #162 ещё открыт на момент снятия. Первая версия признака
  (task_ref.extract_task_refs — любое упоминание #N) на этих же живых данных
  дала бы ложное срабатывание: пометила бы #216 протухшей, хотя блокировка
  законна, пока открыт #162. Тест ниже доказывает, что суженный маркерный
  признак этого не делает.

Проводка stale_blocked_check — на моке pulse_guard.gh (единственный пункт
патча, как у upstream_drift_check), сеть не нужна.

Запуск: python -m pytest scripts/orchestra/test_stale_blocked_guard.py -q
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))

SCRIPT = _DIR / "stale_blocked_guard.py"
spec = importlib.util.spec_from_file_location("stale_blocked_guard", SCRIPT)
sbg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sbg)  # type: ignore[union-attr]

pg = sys.modules["pulse_guard"]

REPO = "mytab0r/edge-harness"

# ── Прод-форма: тело и комментарий #268 (сняты 2026-09-05 живым gh) ─────────────

ISSUE_268_BODY = (
    "## Цель\n\nРеализовать `.github/workflows/plugin-forge.yml` — форж плагинов "
    "в раннере (эпик #77, третья стройка «Форж и канал обновления», "
    "`openspec/changes/dsh-edge-plugin-system/design.md`).\n\n"
    "## Зависимости\n\nЗависит от #265 (`check-plugin-compat.mjs`) — форж использует "
    "чекер как обязательный первый гейт, без него неоткуда взять шаг совместимости.\n"
)

ISSUE_268_ESCALATION_COMMENT = (
    "Блокирована: #265\n\n"
    "Форж плагинов использует `check-plugin-compat.mjs` как обязательный первый гейт "
    "совместимости. Без #265 нечего выполнять в гейте.\n\n"
    "Газ: слияние PR #265 (и переход в open state в исходной задаче #265) — проверить "
    "через `gh issue view 265 --json state`."
)

# Прод-форма: тело issue #216 (сняты 2026-09-05 живым gh) — три номера союзом
# «И», БЕЗ маркера «Блокирована: #N». #162 открыт, #163/#164 закрыты на
# момент снятия — легитимный «живой» кандидат на ложное срабатывание.
ISSUE_216_BODY = (
    "## Зачем\n\nПровайдеры падают перемежающимися сбоями. Решение владельца "
    "2026-09-02: плагин пишем и деплоим мы сами, внешнего артефакта нет. "
    "Подробности — в #215.\n\n"
    "## Блокирующая зависимость\n\nСобрать и задеплоить плагин нечем, пока не "
    "слита плагинная машинерия эпика #77: PR #162 (plugin-forge), #163 "
    "(дизайн), #164 (PoC hello-world через forge → deploy → UI). Это "
    "зависимость, а не пожелание — сначала машинерия, потом плагин.\n"
)


def issue_268(labels=("task", "area:worker", "blocked")):
    return {
        "number": 268,
        "labels": [{"name": name} for name in labels],
        "body": ISSUE_268_BODY,
        "comments_text": [
            "♻️ Задача возвращена в пул оркестратором: PR #273 нездоров дольше 120 мин.",
            ISSUE_268_ESCALATION_COMMENT,
        ],
    }


def issue_216():
    return {
        "number": 216,
        "labels": [{"name": "task"}, {"name": "blocked"}],
        "body": ISSUE_216_BODY,
        "comments_text": [],
    }


# ── Чистое решение ────────────────────────────────────────────────────────────


def test_stale_marker_targets_finds_only_marked_number_in_268():
    texts = [ISSUE_268_BODY, ISSUE_268_ESCALATION_COMMENT]
    # #77 и «Зависит от #265» в теле — БЕЗ маркера «Блокирована:», не считаются;
    # ровно один номер приходит из маркера в комментарии.
    assert sbg.stale_marker_targets(268, texts) == [265]


def test_stale_marker_targets_finds_nothing_in_216_prose_conjunction():
    """Живой контрпример: #216 называет #162/#163/#164 союзом «И», без
    маркера «Блокирована: #N» — признак не срабатывает вовсе."""
    assert sbg.stale_marker_targets(216, [ISSUE_216_BODY]) == []


def test_stale_marker_targets_excludes_self_and_dedupes():
    texts = ["Блокирована: #265\n…\nБлокирована: #265", "Блокирована: #268"]
    assert sbg.stale_marker_targets(268, texts) == [265]


def test_find_stale_blocked_flags_268_on_prod_form_with_265_closed():
    """Мутация (а): #265 закрыт (прод-форма факта) — #268 обязана попасть в отчёт."""
    violations = sbg.find_stale_blocked([issue_268()], closed_numbers={265})
    assert violations == [{"number": 268, "stale_refs": [265]}]


def test_find_stale_blocked_silent_while_265_still_open():
    """Мутация (б): та же #268, но #265 ещё открыт — нарушения нет (условие
    специфично к состоянию ссылки, не к самому факту ссылки)."""
    violations = sbg.find_stale_blocked([issue_268()], closed_numbers=set())
    assert violations == []


def test_find_stale_blocked_ignores_issue_without_blocked_label():
    issue = issue_268(labels=("task", "area:worker"))
    violations = sbg.find_stale_blocked([issue], closed_numbers={265})
    assert violations == []


def test_find_stale_blocked_no_false_positive_on_216_even_with_163_164_closed():
    """Живой класс ложного срабатывания: #163 и #164 закрыты, #162 (не
    маркированный, союз «И» в прозе) ещё открыт — #216 НЕ попадает в отчёт,
    хотя закрытые номера присутствуют в closed_numbers."""
    violations = sbg.find_stale_blocked([issue_216()], closed_numbers={163, 164})
    assert violations == []


def test_find_stale_blocked_silent_on_blocked_issue_without_any_reference():
    """Законный ручной случай (LABELS.md): эскалация «нужен секрет владельца»
    без ссылки на другой issue — не флагуется, это не машинно проверяемо."""
    issue = {
        "number": 6,
        "labels": [{"name": "task"}, {"name": "blocked"}],
        "body": "## Цель\nУзкий PAT для dispatch.",
        "comments_text": ["Упёрся в то, что есть только у владельца: fine-grained PAT "
                           "создаётся только через веб-интерфейс."],
    }
    violations = sbg.find_stale_blocked([issue], closed_numbers={265})
    assert violations == []


def test_violation_text_names_issue_and_stale_refs():
    text = sbg.violation_text({"number": 268, "stale_refs": [265]})
    assert "#268" in text and "#265" in text and "blocked" in text


# ── Чистое решение: маркер эскалации (находка ревью #333/#336) ─────────────────


def test_escalation_target_is_stable_sorted_refs():
    assert sbg.escalation_target({"number": 268, "stale_refs": [265]}) == "#265"
    assert sbg.escalation_target({"number": 1, "stale_refs": [3, 5]}) == "#3,#5"


def test_escalation_marker_target_reads_body():
    body = "🚨 [протухшая блокировка: #265]\nтекст"
    assert sbg.escalation_marker_target(body) == "#265"


def test_escalation_marker_target_none_without_marker():
    assert sbg.escalation_marker_target("обычный комментарий без маркера") is None


def test_already_escalated_true_for_same_target():
    texts = ["шум", "🚨 [протухшая блокировка: #265]\nсигнал"]
    assert sbg.already_escalated(texts, "#265") is True


def test_already_escalated_false_for_different_target():
    """Набор протухших ссылок сменился (новая ссылка протухла/старая ушла) —
    это новый эпизод, старый маркер не гасит новый сигнал."""
    texts = ["🚨 [протухшая блокировка: #163]\nсигнал"]
    assert sbg.already_escalated(texts, "#265") is False


def test_escalation_text_puts_marker_first_line():
    text = sbg.escalation_text({"number": 268, "stale_refs": [265]})
    assert text.startswith("🚨 [протухшая блокировка: #265]")
    assert "#268" in text


# ── Проводка: gh замокан, сеть не нужна ─────────────────────────────────────────


def patch_gh(monkeypatch, fake):
    monkeypatch.setattr(pg, "gh", fake)


def test_stale_blocked_check_zero_violations_is_quiet():
    """Холостой ход: нет issue с меткой blocked."""
    def fake(*args):
        if args[0].startswith("repos/mytab0r/edge-harness/issues?state=open&labels=blocked"):
            return []
        raise AssertionError(f"неожиданный вызов gh: {args}")

    original = pg.gh
    pg.gh = fake
    try:
        lines = sbg.stale_blocked_check(REPO)
    finally:
        pg.gh = original
    assert lines == ["💗 blocked: протухших меток не найдено (0 issue с меткой blocked проверено)"]


@pytest.fixture()
def offline_telegram(monkeypatch):
    """Без секретов Telegram честно «не доставлен» — сетью тест не ходит
    (тот же приём, что test_upstream_drift.offline_telegram)."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)


def test_stale_blocked_check_reports_268_on_live_shaped_responses(monkeypatch, offline_telegram):
    """Полная проводка на прод-форме: listing → comments → issues/265 (closed)
    → эскалация (комментарий в #268 + Telegram, находка ревью #333/#336)."""
    calls: list[tuple] = []

    def fake(*args):
        calls.append(args)
        if args[0] == "-X":
            return None
        url = args[0]
        if url.startswith("repos/mytab0r/edge-harness/issues?state=open&labels=blocked"):
            return [{
                "number": 268,
                "labels": [{"name": "task"}, {"name": "area:worker"}, {"name": "blocked"}],
                "body": ISSUE_268_BODY,
            }]
        if url == "repos/mytab0r/edge-harness/issues/268/comments?per_page=100&page=1":
            return [{"body": ISSUE_268_ESCALATION_COMMENT}]
        if url == "repos/mytab0r/edge-harness/issues/265":
            return {"state": "closed"}
        raise AssertionError(f"неожиданный вызов gh: {args}")

    patch_gh(monkeypatch, fake)
    lines = sbg.stale_blocked_check(REPO)
    assert len(lines) == 1
    assert lines[0].startswith("🚨")
    assert "#268" in lines[0] and "#265" in lines[0]

    posted = [c for c in calls if c[0] == "-X" and c[1] == "POST"]
    comment_calls = [c for c in posted if "issues/268/comments" in c[2]]
    assert comment_calls, "эскалация обязана оставить след прямо в #268 (не в отдельной задаче-статус)"
    body_arg = next(a for a in comment_calls[0] if a.startswith("body="))
    assert "[протухшая блокировка: #265]" in body_arg, "маркер эпизода — в теле комментария"


def test_stale_blocked_check_silent_channel_when_episode_already_escalated(monkeypatch, offline_telegram):
    """Тот же набор протухших ссылок уже сигналился в #268 (маркер найден в
    уже прочитанных комментариях) — второй прогон не шлёт повтор в канал
    (иначе 15-минутный крон заспамит), но нарушение остаётся в отчёте
    (не 💗) — CI-шаг не должен выглядеть холостым."""
    prior_escalation = "🚨 [протухшая блокировка: #265]\nуже сигналили раньше"

    def fake(*args):
        if args[0] == "-X":
            raise AssertionError(f"мутирующий вызов не ожидался — эпизод уже сигналился: {args}")
        url = args[0]
        if url.startswith("repos/mytab0r/edge-harness/issues?state=open&labels=blocked"):
            return [{
                "number": 268,
                "labels": [{"name": "task"}, {"name": "area:worker"}, {"name": "blocked"}],
                "body": ISSUE_268_BODY,
            }]
        if url == "repos/mytab0r/edge-harness/issues/268/comments?per_page=100&page=1":
            return [{"body": ISSUE_268_ESCALATION_COMMENT}, {"body": prior_escalation}]
        if url == "repos/mytab0r/edge-harness/issues/265":
            return {"state": "closed"}
        raise AssertionError(f"неожиданный вызов gh: {args}")

    patch_gh(monkeypatch, fake)
    lines = sbg.stale_blocked_check(REPO)
    assert len(lines) == 1
    assert lines[0].startswith("🔇")
    assert "#268" in lines[0] and "#265" in lines[0]


def test_stale_blocked_check_silent_on_216_live_shaped_responses(monkeypatch):
    """Та же проводка, но на форме #216: #163/#164 закрыты — отчёт обязан
    остаться холостым (живой контрпример ложного срабатывания)."""
    def fake(*args):
        url = args[0]
        if url.startswith("repos/mytab0r/edge-harness/issues?state=open&labels=blocked"):
            return [{
                "number": 216,
                "labels": [{"name": "task"}, {"name": "blocked"}],
                "body": ISSUE_216_BODY,
            }]
        if url == "repos/mytab0r/edge-harness/issues/216/comments?per_page=100&page=1":
            return []
        raise AssertionError(f"неожиданный вызов gh: {args}")

    patch_gh(monkeypatch, fake)
    lines = sbg.stale_blocked_check(REPO)
    assert lines == ["💗 blocked: протухших меток не найдено (1 issue с меткой blocked проверено)"]


def test_main_exit_code_reflects_violations(monkeypatch):
    monkeypatch.setattr(sbg, "stale_blocked_check", lambda repo: ["🚨 нарушение"])
    assert sbg.main() == 1

    monkeypatch.setattr(sbg, "stale_blocked_check", lambda repo: ["💗 всё чисто"])
    assert sbg.main() == 0


def test_main_exit_code_treats_silent_channel_as_violation_too(monkeypatch):
    """Находка ревью #333/#336: «уже эскалировано, повтор не шлём» — это всё
    ещё нарушение (метка не снята), а не холостой ход. Молчит только канал
    эскалации, CI-шаг остаётся красным."""
    monkeypatch.setattr(sbg, "stale_blocked_check", lambda repo: ["🔇 нарушение, но эпизод уже сигналился"])
    assert sbg.main() == 1
