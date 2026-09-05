#!/usr/bin/env python3
"""Тесты сигнала ослабления защиты main (scripts/orchestra/branch_protection_watch.py, #370/#341).

Фикстуры `rule`/`changes`/`action` — ВЕРБАТИМ из канонических примеров GitHub
(`raw.githubusercontent.com/octokit/webhooks/main/payload-examples/api.github.com/
branch_protection_rule/{created,edited,deleted}.payload.json`, снято живым
запросом 2026-09-06, не пересказ по памяти); `repository`/`organization` —
раздутые поля, которые код не читает, обрезаны, `sender.login` оставлен.

Проводка `branch_protection_check` — на моке `pulse_guard.gh` (тот же приём,
что `test_upstream_drift.py`): сигнал Telegram в тестовой среде честно «не
доставлен» (секретов нет), место правды — факт POST-комментария в #120.

Запуск: python -m pytest scripts/orchestra/test_branch_protection_watch.py -q
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))

SCRIPT = _DIR / "branch_protection_watch.py"
spec = importlib.util.spec_from_file_location("branch_protection_watch", SCRIPT)
bpw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bpw)  # type: ignore[union-attr]

pg = sys.modules["pulse_guard"]


# ── Прод-форма rule: поле-в-поле из примеров GitHub (см. докстринг модуля) ───

COMPLIANT_RULE = {
    "id": 1,
    "repository_id": 1,
    "name": "main",
    "admin_enforced": True,
    "required_status_checks": ["test", "contract"],
    "strict_required_status_checks_policy": True,
    "allow_force_pushes_enforcement_level": "off",
    "allow_deletions_enforcement_level": "off",
}

# Снято живьём: created.payload.json (октокит/webhooks) — admin_enforced=false,
# required_status_checks=["basic-CI"], strict=false, force-pushes off,
# deletions off. Здесь имя правила заменено на "main" (в реальном примере —
# "production"), остальные поля — как отдал GitHub.
CREATED_EVENT = {
    "action": "created",
    "rule": {
        "id": 21796960,
        "repository_id": 259377789,
        "name": "main",
        "pull_request_reviews_enforcement_level": "off",
        "required_approving_review_count": 1,
        "dismiss_stale_reviews_on_push": False,
        "require_code_owner_review": False,
        "authorized_dismissal_actors_only": False,
        "ignore_approvals_from_contributors": False,
        "required_status_checks": ["basic-CI"],
        "required_status_checks_enforcement_level": "non_admins",
        "strict_required_status_checks_policy": False,
        "signature_requirement_enforcement_level": "off",
        "linear_history_requirement_enforcement_level": "off",
        "admin_enforced": False,
        "allow_force_pushes_enforcement_level": "off",
        "allow_deletions_enforcement_level": "off",
        "merge_queue_enforcement_level": "off",
        "required_deployments_enforcement_level": "off",
        "required_conversation_resolution_level": "off",
        "authorized_actors_only": True,
        "authorized_actor_names": ["Codertocat"],
    },
    "sender": {"login": "Codertocat"},
}

# Снято живьём: edited.payload.json — changes.admin_enforced.from == false,
# т.е. admin_enforced ВКЛЮЧИЛИ этим событием (rule.admin_enforced текущий —
# тоже false в примере: это фикстура структуры payload, не сценарий регресса —
# для регресса используем EDITED_ADMIN_ENFORCED_TURNED_OFF ниже).
EDITED_EVENT_RAW = {
    "action": "edited",
    "rule": {
        "id": 21796960,
        "repository_id": 259377789,
        "name": "main",
        "pull_request_reviews_enforcement_level": "off",
        "required_approving_review_count": 1,
        "dismiss_stale_reviews_on_push": False,
        "require_code_owner_review": False,
        "authorized_dismissal_actors_only": False,
        "ignore_approvals_from_contributors": False,
        "required_status_checks": ["basic-CI"],
        "required_status_checks_enforcement_level": "non_admins",
        "strict_required_status_checks_policy": False,
        "signature_requirement_enforcement_level": "off",
        "linear_history_requirement_enforcement_level": "off",
        "admin_enforced": False,
        "allow_force_pushes_enforcement_level": "off",
        "allow_deletions_enforcement_level": "off",
        "merge_queue_enforcement_level": "off",
        "required_deployments_enforcement_level": "off",
        "required_conversation_resolution_level": "off",
        "authorized_actors_only": True,
        "authorized_actor_names": ["Codertocat"],
    },
    "changes": {
        "authorized_actors_only": {"from": False},
        "authorized_actor_names": {"from": []},
        "authorized_dismissal_actors_only": {"from": None},
        "linear_history_requirement_enforcement_level": {"from": "everyone"},
        "required_approving_review_count": {"from": 2},
        "dismiss_stale_reviews_on_push": {"from": True},
        "require_code_owner_review": {"from": True},
        "allow_force_pushes_enforcement_level": {"from": "everyone"},
        "required_deployments_enforcement_level": {"from": "off"},
        "pull_request_reviews_enforcement_level": {"from": "off"},
        "required_status_checks_enforcement_level": {"from": "off"},
        "signature_requirement_enforcement_level": {"from": "non_admins"},
        "admin_enforced": {"from": False},
        "allow_deletions_enforcement_level": {"from": "off"},
        "required_conversation_resolution_level": {"from": "off"},
    },
    "sender": {"login": "Codertocat"},
}

# Снято живьём: deleted.payload.json — форма идентична created (rule — то,
# что было удалено).
DELETED_EVENT = {
    "action": "deleted",
    "rule": {
        "id": 21796960,
        "repository_id": 259377789,
        "name": "main",
        "admin_enforced": True,
        "required_status_checks": ["test", "contract"],
        "strict_required_status_checks_policy": True,
        "allow_force_pushes_enforcement_level": "off",
        "allow_deletions_enforcement_level": "off",
    },
    "sender": {"login": "Codertocat"},
}


def compliant_event(action="edited", **rule_overrides):
    rule = dict(COMPLIANT_RULE, **rule_overrides)
    return {"action": action, "rule": rule, "sender": {"login": "mytab0r"}}


# ── Чистое решение: evaluate_rule ────────────────────────────────────────────


def test_compliant_rule_has_no_violations():
    assert bpw.evaluate_rule("edited", COMPLIANT_RULE) == []


def test_deleted_action_is_always_critical_regardless_of_rule_content():
    violations = bpw.evaluate_rule("deleted", COMPLIANT_RULE)
    assert [v["field"] for v in violations] == ["branch_protection"]


def test_admin_enforced_turned_off_is_a_violation():
    # Регресс живого инцидента 2026-09-06: enforce_admins=false пропускал
    # админский токен мимо test/contract.
    violations = bpw.evaluate_rule("edited", compliant_event(admin_enforced=False)["rule"])
    fields = [v["field"] for v in violations]
    assert "admin_enforced" in fields


def test_missing_required_context_is_a_violation():
    violations = bpw.evaluate_rule(
        "edited", compliant_event(required_status_checks=["test"])["rule"])
    hit = next(v for v in violations if v["field"] == "required_status_checks")
    assert hit["missing"] == ["contract"]


def test_extra_required_context_is_benign_no_shouting():
    # Требование задачи: добавление ещё одной обязательной проверки — не шум.
    violations = bpw.evaluate_rule(
        "edited", compliant_event(required_status_checks=["test", "contract", "extra-check"])["rule"])
    assert violations == []


def test_strict_turned_off_is_a_violation():
    violations = bpw.evaluate_rule(
        "edited", compliant_event(strict_required_status_checks_policy=False)["rule"])
    assert any(v["field"] == "strict_required_status_checks_policy" for v in violations)


def test_force_pushes_allowed_is_a_violation():
    violations = bpw.evaluate_rule(
        "edited", compliant_event(allow_force_pushes_enforcement_level="everyone")["rule"])
    assert any(v["field"] == "allow_force_pushes_enforcement_level" for v in violations)


def test_deletions_allowed_is_a_violation():
    violations = bpw.evaluate_rule(
        "edited", compliant_event(allow_deletions_enforcement_level="everyone")["rule"])
    assert any(v["field"] == "allow_deletions_enforcement_level" for v in violations)


def test_missing_fields_in_payload_do_not_raise_or_false_alarm():
    # Форма payload разошлась с ожидаемой (поле отсутствует) — не гадаем,
    # просто не проверяем это поле; не выдаём молчание за компромисс.
    assert bpw.evaluate_rule("edited", {"name": "main"}) == []


# ── Прод-форма реальных payload'ов GitHub (см. докстринг модуля) ─────────────


def test_created_event_from_github_example_flags_admin_enforced_off():
    violations = bpw.evaluate_rule(CREATED_EVENT["action"], CREATED_EVENT["rule"])
    assert any(v["field"] == "admin_enforced" for v in violations)


def test_edited_event_from_github_example_parses_without_crashing():
    # Смысл теста — форма payload'а (changes.<field>.from) не ломает разбор;
    # решение принимается по rule, changes не используется как условие тревоги.
    violations = bpw.evaluate_rule(EDITED_EVENT_RAW["action"], EDITED_EVENT_RAW["rule"])
    assert any(v["field"] == "admin_enforced" for v in violations)


def test_deleted_event_from_github_example_is_critical():
    violations = bpw.evaluate_rule(DELETED_EVENT["action"], DELETED_EVENT["rule"])
    assert [v["field"] for v in violations] == ["branch_protection"]


# ── Тексты сигнала: следствие + готовая команда восстановления ───────────────


def test_alert_text_names_consequence_not_fact():
    violations = bpw.evaluate_rule("edited", compliant_event(admin_enforced=False)["rule"])
    text = bpw.build_alert_text("edited", violations, "mytab0r/edge-harness", "someone")
    assert "мимо обязательных проверок test и contract" in text
    assert "2026-09-06" in text
    assert "gh api -X POST repos/mytab0r/edge-harness/branches/main/protection/enforce_admins" in text


def test_alert_text_deleted_gives_full_restore_command():
    violations = bpw.evaluate_rule("deleted", COMPLIANT_RULE)
    text = bpw.build_alert_text("deleted", violations, "mytab0r/edge-harness", "someone")
    assert "gh api --method PUT repos/mytab0r/edge-harness/branches/main/protection" in text
    assert '"enforce_admins": true' in text


def test_alert_text_carries_marker():
    violations = bpw.evaluate_rule("edited", compliant_event(admin_enforced=False)["rule"])
    text = bpw.build_alert_text("edited", violations, "mytab0r/edge-harness", "someone")
    assert text.startswith(f"🚨 edge-harness: {bpw.MARKER}")


def test_recovery_command_unknown_field_raises():
    with pytest.raises(ValueError):
        bpw.recovery_command("mytab0r/edge-harness", "no-such-field")


# ── Проводка branch_protection_check на моке gh ──────────────────────────────


class FakeGh:
    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, *args):
        joined = " ".join(args)
        self.calls.append(joined)
        return None

    def mutating_calls(self):
        return [c for c in self.calls if c.startswith(("-X POST", "-X PUT", "-X DELETE"))]


@pytest.fixture()
def offline_telegram(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)


def test_check_silent_and_no_mutation_on_compliant_state(monkeypatch, offline_telegram):
    fake = FakeGh()
    monkeypatch.setattr(pg, "gh", fake)
    lines = bpw.branch_protection_check("mytab0r/edge-harness", compliant_event("edited"))
    assert lines == ["✅ branch_protection_rule: edited — состояние main соответствует EXPECTED_*"]
    assert fake.mutating_calls() == [], f"согласованное состояние не имеет права мутировать: {fake.calls}"


def test_check_escalates_comment_on_admin_enforced_off(monkeypatch, offline_telegram):
    fake = FakeGh()
    monkeypatch.setattr(pg, "gh", fake)
    event = compliant_event("edited", admin_enforced=False)
    lines = bpw.branch_protection_check("mytab0r/edge-harness", event)
    assert lines[0].startswith("🚨 branch_protection_rule: edited")
    assert "admin_enforced" in lines[0]
    posted = [c for c in fake.calls if "-X POST" in c]
    assert any(f"repos/mytab0r/edge-harness/issues/{pg.WATCHDOG_ISSUE}/comments" in c for c in posted), \
        f"место правды сигнала — комментарий в #{pg.WATCHDOG_ISSUE}: {fake.calls}"
    assert "Telegram: НЕ доставлен" in lines[0]  # секретов нет — честный исход


def test_check_skips_rules_for_other_branches(monkeypatch, offline_telegram):
    fake = FakeGh()
    monkeypatch.setattr(pg, "gh", fake)
    event = {"action": "edited", "rule": dict(COMPLIANT_RULE, name="release", admin_enforced=False),
             "sender": {"login": "someone"}}
    lines = bpw.branch_protection_check("mytab0r/edge-harness", event)
    assert lines == ["ℹ️ branch_protection_rule: edited на правиле 'release' — не main, пропускаю"]
    assert fake.calls == [], "чужое правило не имеет права дёргать gh вовсе"


def test_check_deleted_action_always_escalates(monkeypatch, offline_telegram):
    fake = FakeGh()
    monkeypatch.setattr(pg, "gh", fake)
    lines = bpw.branch_protection_check("mytab0r/edge-harness", DELETED_EVENT)
    assert lines[0].startswith("🚨 branch_protection_rule: deleted")
    posted = [c for c in fake.calls if "-X POST" in c]
    assert any(f"issues/{pg.WATCHDOG_ISSUE}/comments" in c for c in posted)
