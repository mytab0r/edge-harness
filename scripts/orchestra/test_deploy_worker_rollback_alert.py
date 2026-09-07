#!/usr/bin/env python3
"""Тесты эскалации автооткота `deploy-worker.yml` (#614).

Проводка — на моке `pulse_guard.gh` (тот же приём, что
`test_branch_protection_watch.py`): `escalate()` постит комментарий в
задачу-статус #120 и best-effort шлёт Telegram; в тестовой среде без секретов
Telegram честно «не доставлен», место правды — факт POST-комментария.

Запуск: python -m pytest scripts/orchestra/test_deploy_worker_rollback_alert.py -q
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))

SCRIPT = _DIR / "deploy_worker_rollback_alert.py"
spec = importlib.util.spec_from_file_location("deploy_worker_rollback_alert", SCRIPT)
dwra = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dwra)  # type: ignore[union-attr]

pg = sys.modules["pulse_guard"]

REPO = "mytab0r/edge-harness"


class FakeGh:
    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, *args):
        self.calls.append(" ".join(args))
        return None


@pytest.fixture()
def offline_telegram(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)


# ── parse_bool_env: форма вывода GitHub Actions ("true"/"false", регистр, пусто) ──


def test_parse_bool_env_true_variants():
    assert dwra.parse_bool_env("true") is True
    assert dwra.parse_bool_env("True") is True
    assert dwra.parse_bool_env("  true ") is True


def test_parse_bool_env_false_and_missing_default_to_false():
    assert dwra.parse_bool_env("false") is False
    assert dwra.parse_bool_env("") is False
    assert dwra.parse_bool_env(None) is False
    assert dwra.parse_bool_env("garbage") is False


# ── Чистая логика: три исхода текста, от тише к громче (см. докстринг модуля) ──


def test_rollback_not_confirmed_is_the_loudest_case():
    text = dwra.rollback_alert_text(REPO, "123", rollback_confirmed=False, post_rollback_ok=False)
    assert "НЕМЕДЛЕННО" in text
    assert "АВТООТКАТ НЕ ПОДТВЕРЖДЁН" in text


def test_rollback_confirmed_but_post_check_still_red_is_the_worst_case():
    # Задача #614, п.1: "если и она красная — это худший случай, он обязан быть громким".
    text = dwra.rollback_alert_text(REPO, "123", rollback_confirmed=True, post_rollback_ok=False)
    assert "НЕМЕДЛЕННО" in text
    assert "откатанная версия не отвечает" in text


def test_rollback_confirmed_and_post_check_green_is_informational_not_alarming():
    text = dwra.rollback_alert_text(REPO, "123", rollback_confirmed=True, post_rollback_ok=True)
    assert "НЕМЕДЛЕННО" not in text
    assert text.startswith("🔙")


def test_two_loud_outcomes_both_carry_the_siren_and_the_same_marker():
    loud_a = dwra.rollback_alert_text(REPO, "1", rollback_confirmed=False, post_rollback_ok=False)
    loud_b = dwra.rollback_alert_text(REPO, "1", rollback_confirmed=True, post_rollback_ok=False)
    quiet = dwra.rollback_alert_text(REPO, "1", rollback_confirmed=True, post_rollback_ok=True)
    for text in (loud_a, loud_b, quiet):
        assert dwra.MARKER in text
    assert loud_a.startswith("🚨")
    assert loud_b.startswith("🚨")
    assert not quiet.startswith("🚨")


def test_alert_text_carries_run_link_built_from_server_url_repo_and_run_id():
    text = dwra.rollback_alert_text(
        REPO, "999", rollback_confirmed=True, post_rollback_ok=True,
        server_url="https://github.example",
    )
    assert "https://github.example/mytab0r/edge-harness/actions/runs/999" in text


def test_alert_text_without_run_id_is_honest_not_a_broken_link():
    text = dwra.rollback_alert_text(REPO, "", rollback_confirmed=True, post_rollback_ok=True)
    assert "без ссылки" in text


# ── Проводка: escalate_rollback идёт тем же каналом, что предохранитель (#120) ──


def test_escalate_rollback_posts_to_watchdog_issue(monkeypatch, offline_telegram):
    fake = FakeGh()
    monkeypatch.setattr(pg, "gh", fake)
    result = dwra.escalate_rollback(REPO, "42", rollback_confirmed=True, post_rollback_ok=False)
    posted = [c for c in fake.calls if "-X POST" in c and "comments" in c]
    assert any(f"repos/{REPO}/issues/{pg.WATCHDOG_ISSUE}/comments" in c for c in posted), \
        f"место правды сигнала — комментарий в #{pg.WATCHDOG_ISSUE}: {fake.calls}"
    assert "Telegram: НЕ доставлен" in result  # секретов нет в тесте — честный исход
    assert f"след в #{pg.WATCHDOG_ISSUE}: оставлен" in result


def test_escalate_rollback_comment_body_names_the_actual_verdict(monkeypatch, offline_telegram):
    fake = FakeGh()
    monkeypatch.setattr(pg, "gh", fake)
    dwra.escalate_rollback(REPO, "42", rollback_confirmed=False, post_rollback_ok=False)
    posted = [c for c in fake.calls if "-X POST" in c and "comments" in c]
    assert any("НЕ ПОДТВЕРЖДЁН" in c for c in posted)


def test_escalate_rollback_does_not_mutate_anything_besides_the_comment(monkeypatch, offline_telegram):
    fake = FakeGh()
    monkeypatch.setattr(pg, "gh", fake)
    dwra.escalate_rollback(REPO, "42", rollback_confirmed=True, post_rollback_ok=True)
    non_comment_mutations = [
        c for c in fake.calls
        if c.startswith(("-X POST", "-X PUT", "-X DELETE", "-X PATCH")) and "comments" not in c
    ]
    assert non_comment_mutations == [], f"сигнал не имеет права мутировать что-то ещё: {fake.calls}"


# ── main(): чтение окружения ровно так, как его передаёт workflow ────────────


def test_main_reads_env_and_escalates_the_worst_case(monkeypatch, offline_telegram):
    fake = FakeGh()
    monkeypatch.setattr(pg, "gh", fake)
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setenv("GITHUB_RUN_ID", "555")
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("ROLLBACK_CONFIRMED", "true")
    monkeypatch.setenv("POST_ROLLBACK_OK", "false")
    assert dwra.main() == 0
    posted = [c for c in fake.calls if "-X POST" in c and "comments" in c]
    assert any("откатанная версия не отвечает" in c for c in posted)


def test_main_reads_env_and_escalates_the_recovered_case(monkeypatch, offline_telegram):
    fake = FakeGh()
    monkeypatch.setattr(pg, "gh", fake)
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setenv("GITHUB_RUN_ID", "556")
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("ROLLBACK_CONFIRMED", "true")
    monkeypatch.setenv("POST_ROLLBACK_OK", "true")
    assert dwra.main() == 0
    posted = [c for c in fake.calls if "-X POST" in c and "comments" in c]
    assert any("🔙" in c for c in posted)


def test_main_requires_github_repository_env(monkeypatch, offline_telegram):
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    with pytest.raises(KeyError):
        dwra.main()
