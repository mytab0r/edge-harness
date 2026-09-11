#!/usr/bin/env python3
"""Тесты наблюдателя алертов Dependabot
(scripts/orchestra/dependabot_alert_watch.py, сирота B аудита 2026-09-11).

Фикстура REAL_SHARP_ALERT — дословный снимок живого открытого алерта этого
репозитория (`gh api repos/mytab0r/edge-harness/dependabot/alerts`,
2026-09-11: пакет `sharp`, GHSA-rgj7-g3m4-5g8c, severity `high`), не
пересказ структуры документации.

Запуск: python -m pytest scripts/orchestra/test_dependabot_alert_watch.py -q
"""

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))  # dependabot_alert_watch.py делает `from pulse_guard import …`

PG_SCRIPT = _DIR / "pulse_guard.py"
pg_spec = importlib.util.spec_from_file_location("pulse_guard", PG_SCRIPT)
pg = importlib.util.module_from_spec(pg_spec)
pg_spec.loader.exec_module(pg)  # type: ignore[union-attr]
sys.modules["pulse_guard"] = pg  # dependabot_alert_watch.py делает `from pulse_guard import …`

SCRIPT = _DIR / "dependabot_alert_watch.py"
spec = importlib.util.spec_from_file_location("dependabot_alert_watch", SCRIPT)
daw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(daw)  # type: ignore[union-attr]


def patch_gh(monkeypatch, fake):
    """dependabot_alert_watch.py импортирует gh/issue_marker_times/escalate
    ИЗ pulse_guard — те, что живут в pulse_guard (issue_marker_times,
    escalate → post_issue_comment/send_telegram), резолвят `gh` через
    __globals__ pulse_guard, поэтому обе привязки должны указывать на один
    и тот же fake (тот же приём, что test_stall_detector.py::patch_gh)."""
    monkeypatch.setattr(daw, "gh", fake)
    monkeypatch.setattr(pg, "gh", fake)


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


class FakeGh:
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


REPO = "mytab0r/edge-harness"
NOW = utc(2026, 9, 11, 12, 0)

# Прод-форма: дословный снимок `gh api repos/mytab0r/edge-harness/
# dependabot/alerts`, 2026-09-11 (см. докстринг модуля), сокращены поля,
# которые ни один потребитель не читает (description/references/cwes) —
# оставлены только те, что читает alert_task_title/alert_task_body/дедуп.
REAL_SHARP_ALERT = {
    "number": 1,
    "state": "open",
    "created_at": "2026-09-08T22:47:09Z",
    "updated_at": "2026-09-08T22:47:09Z",
    "html_url": "https://github.com/mytab0r/edge-harness/security/dependabot/1",
    "dependency": {
        "package": {"ecosystem": "npm", "name": "sharp"},
        "manifest_path": "cf-worker/package-lock.json",
        "relationship": "transitive",
        "scope": "development",
    },
    "security_advisory": {
        "ghsa_id": "GHSA-rgj7-g3m4-5g8c",
        "severity": "high",
        "summary": "sharp: Vulnerabilities in libheif: GHSA-g89c-p67h-r497 and GHSA-2jg2-4ch7-h545",
    },
    "security_vulnerability": {
        "package": {"ecosystem": "npm", "name": "sharp"},
        "severity": "high",
        "vulnerable_version_range": "< 0.35.4",
        "first_patched_version": {"identifier": "0.35.4"},
    },
}


def alert(number, package="sharp", severity="high", patched="0.35.4"):
    a = {
        "number": number,
        "state": "open",
        "html_url": f"https://github.com/mytab0r/edge-harness/security/dependabot/{number}",
        "dependency": {"package": {"name": package}, "manifest_path": "cf-worker/package-lock.json"},
        "security_advisory": {"severity": severity, "summary": "summary"},
        "security_vulnerability": {
            "first_patched_version": ({"identifier": patched} if patched else None),
        },
    }
    return a


def task_issue(number, alert_number, created_at="2026-09-11T00:00:00Z", labels=("task", "dependabot-alert")):
    return {
        "number": number,
        "created_at": created_at,
        "labels": [{"name": n} for n in labels],
        "body": f"...\n{daw.ALERT_FINGERPRINT_MARKER}{alert_number} -->\n",
    }


# ── Права: фикстура прод-формы читается функциями модуля без падения ───────

def test_alert_task_title_and_body_use_real_fixture_fields():
    title = daw.alert_task_title(REAL_SHARP_ALERT)
    body = daw.alert_task_body(REAL_SHARP_ALERT)
    assert title == "Dependabot alert #1: sharp (high)"
    assert "cf-worker/package-lock.json" in body
    assert "обновить `sharp` до версии 0.35.4" in body
    assert "https://github.com/mytab0r/edge-harness/security/dependabot/1" in body
    assert f"{daw.ALERT_FINGERPRINT_MARKER}1 -->" in body


def test_remediation_text_honest_when_no_patch_yet():
    unpatched = alert(2, patched=None)
    text = daw.remediation_text(unpatched)
    assert "патч ещё не выпущен" in text


# ── Дедуп: алерт уже отслеживается — вторая задача не заводится ────────────

def test_dependabot_alert_watch_does_not_duplicate_tracked_alert(monkeypatch):
    existing = task_issue(500, alert_number=1)
    fake = FakeGh({
        "dependabot/alerts?state=open": [REAL_SHARP_ALERT],
        "issues?state=open&labels=dependabot-alert": [existing],
        "issues?state=all&labels=dependabot-alert": [existing],
    })
    patch_gh(monkeypatch, fake)

    observations, actions = daw.dependabot_alert_watch(REPO, NOW)

    assert actions == []
    assert not any("POST" in c and "repos/mytab0r/edge-harness/issues" in c and "labels" not in c
                   for c in fake.calls)


def test_dependabot_alert_watch_creates_task_for_new_alert(monkeypatch):
    created = {"number": 700}
    fake = FakeGh({
        "dependabot/alerts?state=open": [REAL_SHARP_ALERT],
        "issues?state=open&labels=dependabot-alert": [],
        "issues?state=all&labels=dependabot-alert": [],
        "-X POST repos/mytab0r/edge-harness/issues": created,
    })
    patch_gh(monkeypatch, fake)

    observations, actions = daw.dependabot_alert_watch(REPO, NOW)

    assert len(actions) == 1
    assert "#1" in actions[0] and "#700" in actions[0]
    post_calls = [c for c in fake.calls if c.startswith("-X POST repos/mytab0r/edge-harness/issues ")]
    assert len(post_calls) == 1
    assert "labels[]=task" in post_calls[0]
    assert "labels[]=dependabot-alert" in post_calls[0]


# ── Потолок в сутки: пятый алерт заводит задачу, шестой — эскалирует ───────

def test_daily_cap_blocks_new_task_creation_and_escalates_once(monkeypatch):
    new_alerts = [alert(n) for n in range(1, daw.DEPENDABOT_WATCH_DAILY_CAP + 2)]
    fake = FakeGh({
        "dependabot/alerts?state=open": new_alerts,
        "issues?state=open&labels=dependabot-alert": [],
        "issues?state=all&labels=dependabot-alert": [
            task_issue(600 + i, alert_number=100 + i) for i in range(daw.DEPENDABOT_WATCH_DAILY_CAP)
        ],
        f"issues/{pg.WATCHDOG_ISSUE}/comments": [],
        "-X POST repos/mytab0r/edge-harness/issues": {"number": 900},
    })
    patch_gh(monkeypatch, fake)
    escalated = []
    monkeypatch.setattr(daw, "escalate", lambda repo, n, text: escalated.append(text) or "posted")

    observations, actions = daw.dependabot_alert_watch(REPO, NOW)

    assert actions == []  # потолок уже исчерпан посчитанными сегодня задачами
    assert any("потолком" in o for o in observations)
    assert len(escalated) == 1
    assert daw.DEPENDABOT_WATCH_CAP_MARKER in escalated[0]


def test_daily_cap_escalation_not_repeated_same_day(monkeypatch):
    fake = FakeGh({
        "dependabot/alerts?state=open": [alert(1)],
        "issues?state=open&labels=dependabot-alert": [],
        "issues?state=all&labels=dependabot-alert": [
            task_issue(600 + i, alert_number=100 + i) for i in range(daw.DEPENDABOT_WATCH_DAILY_CAP)
        ],
        f"issues/{pg.WATCHDOG_ISSUE}/comments": [
            {"created_at": "2026-09-11T09:00:00Z",
             "body": f"{daw.DEPENDABOT_WATCH_CAP_MARKER} {NOW.date().isoformat()}]\n..."},
        ],
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(daw, "escalate", lambda *a: pytest.fail("уже сигналили сегодня — повтор не нужен"))

    observations, actions = daw.dependabot_alert_watch(REPO, NOW)

    assert any("потолком" in o for o in observations)


# ── Газ: алерт больше не open — задача закрывается сама ────────────────────

def test_resolved_alert_closes_its_task_with_state_comment(monkeypatch):
    tracked = task_issue(500, alert_number=1)
    fake = FakeGh({
        "dependabot/alerts?state=open": [],  # алерт #1 больше не среди открытых
        "issues?state=open&labels=dependabot-alert": [tracked],
        "dependabot/alerts/1": {"number": 1, "state": "fixed"},
        "issues/500/comments": None,
        "-X PATCH repos/mytab0r/edge-harness/issues/500": None,
    })
    patch_gh(monkeypatch, fake)

    observations, actions = daw.dependabot_alert_watch(REPO, NOW)

    assert len(actions) == 1
    assert "#500" in actions[0] and "fixed" in actions[0]
    patch_calls = [c for c in fake.calls if c.startswith("-X PATCH")]
    assert len(patch_calls) == 1
    assert "state=closed" in patch_calls[0]
    comment_calls = [c for c in fake.calls if "issues/500/comments" in c and "-X POST" in c]
    assert len(comment_calls) == 1
    assert "fixed" in comment_calls[0]


def test_still_open_tracked_alert_is_not_touched(monkeypatch):
    tracked = task_issue(500, alert_number=1)
    fake = FakeGh({
        "dependabot/alerts?state=open": [REAL_SHARP_ALERT],  # алерт #1 всё ещё open
        "issues?state=open&labels=dependabot-alert": [tracked],
        "issues?state=all&labels=dependabot-alert": [tracked],
    })
    patch_gh(monkeypatch, fake)

    observations, actions = daw.dependabot_alert_watch(REPO, NOW)

    assert actions == []
    assert not any(c.startswith("-X PATCH") for c in fake.calls)


# ── Мутация, доказывающая гвардию: без tracked_alert_numbers/дедупа по
# номеру алерта каждый пульс завёл бы вторую задачу на тот же #1 — снять
# фильтр `if fingerprint in ci_fingerprints`-эквивалент (здесь —
# `if number in tracked`) и увидеть красный тест выше
# (test_dependabot_alert_watch_does_not_duplicate_tracked_alert). ──────────

def test_tracked_alert_numbers_reads_fingerprint_marker_from_body():
    issues = [task_issue(500, alert_number=1), task_issue(501, alert_number=2)]
    result = daw.tracked_alert_numbers(issues)
    assert result[1]["number"] == 500
    assert result[2]["number"] == 501


def test_tracked_alert_numbers_ignores_issue_without_marker():
    plain = {"number": 999, "body": "обычная задача без отпечатка алерта"}
    assert daw.tracked_alert_numbers([plain]) == {}


# ── Право/транспорт: 403 на списке алертов обязан красить прогон ───────────
# (находка ревью PR #964, критик, блокер 3: main() раньше возвращал 0 всегда,
# независимо от исхода — 403 тонул тихим ⚠️ в зелёном step summary).

def test_dependabot_alert_watch_propagates_alert_list_read_failure(monkeypatch):
    fake = FakeGh({"dependabot/alerts?state=open": RuntimeError("gh api dependabot/alerts: HTTP 403")})
    patch_gh(monkeypatch, fake)

    with pytest.raises(RuntimeError, match="403"):
        daw.dependabot_alert_watch(REPO, NOW)


def test_main_returns_nonzero_when_alert_list_read_fails(monkeypatch):
    fake = FakeGh({"dependabot/alerts?state=open": RuntimeError("gh api dependabot/alerts: HTTP 403")})
    patch_gh(monkeypatch, fake)
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    assert daw.main() == 1


def test_main_returns_zero_on_healthy_run(monkeypatch):
    fake = FakeGh({
        "dependabot/alerts?state=open": [],
        "issues?state=open&labels=dependabot-alert": [],
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    assert daw.main() == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
