#!/usr/bin/env python3
"""Тесты само-аудита здоровья конвейера (scripts/orchestra/health_audit.py,
openspec/changes/pipeline-health-self-audit).

Прод-форма: issues/comments — та же форма ответа GitHub API, что уже
используют fixtures test_stall_detector.py (число, body, created_at,
labels, опциональный pull_request). Маршрутизация вызовов gh — тот же
`FakeGh`, что test_stall_detector.py, независимая копия (см. её докстринг —
разные детекторы, разная метка/дедуп, общий тестовый приём).

Запуск: python -m pytest scripts/orchestra/test_health_audit.py -q
"""

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))

PG_SPEC = importlib.util.spec_from_file_location("pulse_guard", _DIR / "pulse_guard.py")
pg = importlib.util.module_from_spec(PG_SPEC)
PG_SPEC.loader.exec_module(pg)  # type: ignore[union-attr]
sys.modules["pulse_guard"] = pg

HR_SPEC = importlib.util.spec_from_file_location("health_regression", _DIR / "health_regression.py")
hr = importlib.util.module_from_spec(HR_SPEC)
HR_SPEC.loader.exec_module(hr)  # type: ignore[union-attr]
sys.modules["health_regression"] = hr

SPEC = importlib.util.spec_from_file_location("health_audit", _DIR / "health_audit.py")
ha = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ha)  # type: ignore[union-attr]


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


REPO = "mytab0r/edge-harness"
NOW = utc(2026, 9, 9, 12, 0)


class FakeGh:
    """Маршрутизатор по подстроке пути — та же форма, что
    scripts/orchestra/test_stall_detector.py::FakeGh."""

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


def patch_gh(monkeypatch, fake):
    monkeypatch.setattr(ha, "pulse_guard", pg)
    monkeypatch.setattr(pg, "gh", fake)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)


def _issue(number, body, created_at="2026-09-01T00:00:00Z", labels=("task", "self-audit")):
    return {
        "number": number,
        "body": body,
        "created_at": created_at,
        "labels": [{"name": name} for name in labels],
    }


def regression(metric="merge_throughput", status="regression", **overrides):
    label, direction = hr.METRICS[metric]
    fields = dict(
        status=status, metric=metric, label=label, direction=direction,
        baseline=10.0, today=4, today_date="2026-09-09", deviation_pct=60.0, streak_days=3,
    )
    fields.update(overrides)
    return hr.Classification(**fields)


# ── Отсутствие целей — ни одного вызова ──────────────────────────────────


def test_no_targets_makes_zero_calls(monkeypatch):
    fake = FakeGh({})  # любой вызов уронит тест
    patch_gh(monkeypatch, fake)
    ok = regression(status="ok")
    insufficient = regression(status="insufficient_data")
    report = ha.run_self_audit(REPO, [ok, insufficient], "ref", NOW)
    assert report == []


# ── Заведение задачи на регрессии, без эскалации ─────────────────────────


def test_creates_task_for_plain_regression(monkeypatch):
    fp = ha.fingerprint_of("merge_throughput")
    fake = FakeGh({
        "issues?state=open&labels=self-audit": [],
        "issues?state=all&labels=self-audit": [],
        "POST repos/mytab0r/edge-harness/issues": {"number": 555},
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(pg, "escalate", lambda *a, **k: pytest.fail("regression не эскалируется"))

    report = ha.run_self_audit(REPO, [regression()], "docs/.../snapshot.jsonl@abc", NOW)
    assert any("#555" in line and "заведена само-аудитом" in line and fp in line for line in report)
    create_call = next(c for c in fake.calls if "POST" in c)
    assert fp in create_call
    assert "self-audit" in create_call and "labels[]=task" in create_call


def test_render_body_carries_required_sections():
    fp = ha.fingerprint_of("merge_throughput")
    body = ha.render_body(fp, regression(), "docs/research/data/pipeline-health.jsonl на data/pipeline-health")
    assert ha._fingerprint_line(fp) in body
    assert "baseline" in body.lower()
    assert "10.0" in body and "4" in body and "60.0" in body
    assert "Снимок" in body
    assert "Честный потолок" in body


# ── Пожар: заведение + немедленная эскалация ─────────────────────────────


def test_fire_creates_task_and_escalates(monkeypatch):
    fake = FakeGh({
        "issues?state=open&labels=self-audit": [],
        "issues?state=all&labels=self-audit": [],
        "POST repos/mytab0r/edge-harness/issues": {"number": 777},
        "issues/777/comments": [],  # issue_marker_times/escalate читают комментарии
    })
    patch_gh(monkeypatch, fake)

    report = ha.run_self_audit(REPO, [regression(status="fire", deviation_pct=100.0)], "ref", NOW)
    assert any("#777" in line and "заведена само-аудитом" in line for line in report)
    assert any("#777" in line and "эскалация пожара" in line for line in report)
    assert any("issues/777/comments" in call for call in fake.calls)


# ── Дедупликация: задача того же отпечатка уже открыта ───────────────────


def test_existing_open_task_gets_comment_not_new_issue(monkeypatch):
    fp = ha.fingerprint_of("merge_throughput")
    existing = _issue(700, f"тело\n\n{ha._fingerprint_line(fp)}\n\nостальное")
    fake = FakeGh({
        "issues?state=open&labels=self-audit": [existing],
        "issues/700/comments": [],
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(pg, "gh", fake)

    def fail_create(*a, **k):
        pytest.fail("задача того же отпечатка уже открыта — вторая не заводится")
    monkeypatch.setattr(ha.pool_issue, "create_pool_issue", fail_create)

    report = ha.run_self_audit(REPO, [regression()], "ref", NOW)
    assert any("#700" in line and "новая улика" in line for line in report)
    assert any("issues/700/comments" in call and "POST" in call for call in fake.calls)


def test_existing_open_task_same_day_evidence_not_reposted(monkeypatch):
    fp = ha.fingerprint_of("merge_throughput")
    c = regression()
    marker = ha._evidence_marker(fp, c.today_date)
    existing = _issue(700, f"тело\n\n{ha._fingerprint_line(fp)}\n\nостальное")
    fake = FakeGh({
        "issues?state=open&labels=self-audit": [existing],
        "issues/700/comments": [
            {"created_at": "2026-09-09T10:00:00Z", "body": f"{marker}\nуже было"},
        ],
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(pg, "post_issue_comment",
                        lambda *a: pytest.fail("улика за этот день уже оставлена — повтор не нужен"))

    report = ha.run_self_audit(REPO, [c], "ref", NOW)
    assert any("#700" in line and "уже оставлена" in line for line in report)


# ── Суточный потолок ──────────────────────────────────────────────────────


def test_daily_cap_blocks_creation_and_escalates_once(monkeypatch):
    already = [
        _issue(100 + i, "тело без отпечатка этой метрики",
              created_at=(NOW - timedelta(hours=1)).isoformat().replace("+00:00", "Z"))
        for i in range(ha.SELF_AUDIT_DAILY_CAP)
    ]
    fake = FakeGh({
        "issues?state=open&labels=self-audit": [],
        "issues?state=all&labels=self-audit": already,
        f"issues/{pg.WATCHDOG_ISSUE}/comments": [],
    })
    patch_gh(monkeypatch, fake)

    def fail_create(*a, **k):
        pytest.fail("потолок исчерпан — создание задачи запрещено")
    monkeypatch.setattr(ha.pool_issue, "create_pool_issue", fail_create)

    report = ha.run_self_audit(REPO, [regression()], "ref", NOW)
    assert any("потолок само-аудита исчерпан" in line for line in report)
    assert any(f"issues/{pg.WATCHDOG_ISSUE}/comments" in call for call in fake.calls)


def test_daily_cap_escalation_deduped_same_day(monkeypatch):
    already = [
        _issue(200 + i, "тело",
              created_at=(NOW - timedelta(hours=1)).isoformat().replace("+00:00", "Z"))
        for i in range(ha.SELF_AUDIT_DAILY_CAP)
    ]
    cap_marker = f"{ha.CAP_EXHAUSTED_MARKER} {NOW.date().isoformat()}]"
    fake = FakeGh({
        "issues?state=open&labels=self-audit": [],
        "issues?state=all&labels=self-audit": already,
        f"issues/{pg.WATCHDOG_ISSUE}/comments": [
            {"created_at": "2026-09-09T00:00:00Z", "body": f"{cap_marker}\nуже сообщено"},
        ],
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(pg, "post_issue_comment",
                        lambda *a: pytest.fail("эскалация потолка уже была сегодня — повтор не нужен"))

    report = ha.run_self_audit(REPO, [regression()], "ref", NOW)
    assert any("уже эскалирован сегодня" in line for line in report)


# ── Отпечатки покрывают весь набор регрессионных метрик ──────────────────


def test_fingerprint_slugs_cover_every_regression_metric():
    assert set(ha.FINGERPRINT_SLUGS) == set(hr.METRICS)


def test_self_audit_daily_cap_equals_metric_count():
    assert ha.SELF_AUDIT_DAILY_CAP == len(hr.METRICS)
