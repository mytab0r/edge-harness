#!/usr/bin/env python3
"""Тесты детектора устойчивого простоя (scripts/orchestra/stall_detector.py, #201).

Кормятся прод-формой:
  - строки отпечатков `gate:pipeline-paused`/`archive:morde-unreachable` —
    дословные строки отчёта оркестратора живого прогона 33691948474
    (шаг «Обход пула и очередь слияний», `gh run view 33691948474 --log`,
    403 при архиве сессии + пауза предохранителя, снято 2026-09-03);
  - остальные строки-источники (`check:red:…`, `gate:no-ai-verdict`,
    `worker:no-pr`) — точные f-string шаблоны scheduler.py (свой формат,
    не пересказ чужого — сверено построчно с scheduler.py на момент
    написания теста);
  - реальные имена обязательных проверок этого репозитория (`test`,
    `contract`) — `gh api repos/mytab0r/edge-harness/branches/main/protection`;
  - формы ответов `issues?...`/`issues/{n}/comments` — как отдаёт GitHub API
    (число, body, created_at, labels, опциональный pull_request).

Запуск: python -m pytest scripts/orchestra/test_stall_detector.py -q
"""

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))  # stall_detector.py делает `from pulse_guard import …`

PG_SCRIPT = _DIR / "pulse_guard.py"
pg_spec = importlib.util.spec_from_file_location("pulse_guard", PG_SCRIPT)
pg = importlib.util.module_from_spec(pg_spec)
pg_spec.loader.exec_module(pg)  # type: ignore[union-attr]
sys.modules["pulse_guard"] = pg  # stall_detector.py делает `from pulse_guard import …`

SCRIPT = _DIR / "stall_detector.py"
spec = importlib.util.spec_from_file_location("stall_detector", SCRIPT)
sd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sd)  # type: ignore[union-attr]


def patch_gh(monkeypatch, fake):
    """stall_detector.py импортирует gh() ИЗ pulse_guard: имя связывается в
    ДВУХ модулях по отдельности (sd.gh и pg.gh — один и тот же объект на
    момент импорта, но разные привязки после monkeypatch). Функции, которые
    ЖИВУТ в pulse_guard (issue_marker_times, escalate→post_issue_comment/
    send_telegram) резолвят `gh` через __globals__ pulse_guard, поэтому обе
    привязки должны указывать на один и тот же fake."""
    monkeypatch.setattr(sd, "gh", fake)
    monkeypatch.setattr(pg, "gh", fake)


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


class FakeGh:
    """Маршрутизатор вызовов gh api по подстроке пути; каждый вызов пишется
    (тот же приём, что test_pulse_guard.py) — нужен и для холостого хода:
    отсутствие записей в .calls доказывает «ни одного вызова»."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls = []

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
NOW = utc(2026, 9, 3, 12, 0)


# ── Извлечение отпечатков (extract_signals) ───────────────────────────────

# Дословные строки из живого прогона 33691948474 (403 архива + пауза
# предохранителя, см. модульный docstring).
REAL_MORDE_403 = (
    "🚨 морда dsh-edge недоступна для архива сессий (возможность сломана, "
    "не отсутствует): логин в морду не удался: HTTP 403"
)
REAL_PIPELINE_PAUSED = (
    "🚨 конвейер на паузе: 3 красных прогонов worker.yml подряд — "
    "диспатч остановлен (уже оповещено, см. #120)"
)


@pytest.mark.parametrize("line,expected_fingerprints", [
    (REAL_PIPELINE_PAUSED, ["gate:pipeline-paused"]),
    (REAL_MORDE_403, ["archive:morde-unreachable"]),
    ("⏸️ #205 — красные проверки: test, contract", ["check:red:test", "check:red:contract"]),
    ("   (отложены: #205 — красные проверки: test)", ["check:red:test"]),
    ("⏸️ PR #205 без вердикта AI 45 мин, но авто-повторов уже 3/3 — не дёргаю снова, нужен человек",
     ["gate:no-ai-verdict"]),
    ("♻️ #205 просрочена (alice), возвращена в пул", ["worker:no-pr"]),
    ("🚨 #205: сессия harness-205 не заархивирована (возможность сломана): DSH_EDGE timeout",
     ["archive:session-failed"]),
])
def test_extract_signals_recognizes_structured_patterns(line, expected_fingerprints):
    signals = sd.extract_signals([line])
    assert [s.fingerprint for s in signals] == expected_fingerprints
    for signal in signals:
        assert signal.evidence == line.strip()  # улика — дословная строка


@pytest.mark.parametrize("line", [
    "Открытых PR: 3",
    "Пул задач: 2 свободно, 1 в работе",
    "✅ PR #205 слит (squash)",
    "👷 воркер уже работает — dispatch не нужен",
    "",
    "   ",
])
def test_extract_signals_ignores_non_warning_lines(line):
    assert sd.extract_signals([line]) == []


def test_warn_fallback_normalizes_numbers_so_same_class_collapses():
    a = sd.extract_signals(["⚠️ PR #205 не обновлён из main после слияния #206: конфликт"])
    b = sd.extract_signals(["⚠️ PR #311 не обновлён из main после слияния #47: конфликт"])
    assert a[0].fingerprint == b[0].fingerprint
    assert a[0].fingerprint.startswith("warn:")
    # но разные по сути предупреждения не склеиваются
    c = sd.extract_signals(["⚠️ обход замков задач не удался: timeout"])
    assert c[0].fingerprint != a[0].fingerprint


# ── Дедупликация: открытая автозадача с тем же отпечатком уже есть ────────

def _issue(number, body, created_at="2026-09-01T00:00:00Z", labels=("task", "auto-detected")):
    return {
        "number": number,
        "body": body,
        "created_at": created_at,
        "labels": [{"name": name} for name in labels],
    }


def test_detect_and_act_comments_existing_task_instead_of_creating_second(monkeypatch):
    existing = _issue(300, "тело\n\nОтпечаток: `gate:pipeline-paused`\n\nостальное")
    fake = FakeGh({"issues?state=open&labels=auto-detected": [existing]})
    patch_gh(monkeypatch, fake)
    commented = []
    monkeypatch.setattr(sd, "post_issue_comment", lambda repo, n, text: commented.append((n, text)))

    def fail_create(*a, **k):
        pytest.fail("не должен заводить вторую задачу — дубликат по отпечатку")
    monkeypatch.setattr(sd, "create_task", fail_create)

    result = sd.detect_and_act(REPO, NOW, [REAL_PIPELINE_PAUSED])
    assert len(commented) == 1
    assert commented[0][0] == 300
    assert "gate:pipeline-paused" in commented[0][1]
    assert any("#300" in line for line in result)


# ── Устойчивость: задача заводится не раньше порога ────────────────────────

def test_first_sighting_only_leaves_marker_no_task_yet(monkeypatch):
    fake = FakeGh({
        "issues?state=open&labels=auto-detected": [],
        "issues/120/comments": [],  # маркеров ещё нет
    })
    patch_gh(monkeypatch, fake)
    posted = []
    monkeypatch.setattr(sd, "post_issue_comment", lambda repo, n, text: posted.append((n, text)))

    def fail_create(*a, **k):
        pytest.fail("первое наблюдение не должно сразу заводить задачу")
    monkeypatch.setattr(sd, "create_task", fail_create)

    result = sd.detect_and_act(REPO, NOW, [REAL_PIPELINE_PAUSED])
    assert len(posted) == 1
    assert posted[0][0] == sd.WATCHDOG_ISSUE
    assert sd._sighting_marker("gate:pipeline-paused") in posted[0][1]
    assert any("замечен впервые" in line for line in result)


def test_marker_younger_than_threshold_still_waits(monkeypatch):
    first_seen = NOW - timedelta(minutes=sd.STALL_PERSIST_MINUTES - 5)
    fake = FakeGh({
        "issues?state=open&labels=auto-detected": [],
        "issues/120/comments": [
            {"created_at": first_seen.isoformat().replace("+00:00", "Z"),
             "body": f"👀 {sd._sighting_marker('gate:pipeline-paused')}\n..."},
        ],
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sd, "post_issue_comment", lambda *a: pytest.fail("не должен писать — не первый раз"))

    def fail_create(*a, **k):
        pytest.fail("порог устойчивости ещё не истёк — рано заводить задачу")
    monkeypatch.setattr(sd, "create_task", fail_create)

    result = sd.detect_and_act(REPO, NOW, [REAL_PIPELINE_PAUSED])
    assert any("держится" in line for line in result)


def test_marker_older_than_threshold_creates_task_with_evidence(monkeypatch):
    first_seen = NOW - timedelta(minutes=sd.STALL_PERSIST_MINUTES + 5)
    fake = FakeGh({
        "issues?state=open&labels=auto-detected": [],
        "issues/120/comments": [
            {"created_at": first_seen.isoformat().replace("+00:00", "Z"),
             "body": f"👀 {sd._sighting_marker('gate:pipeline-paused')}\n..."},
        ],
        "issues?state=all&labels=auto-detected": [],  # для суточного потолка
        "POST repos/mytab0r/edge-harness/issues": {"number": 999},
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sd, "post_issue_comment", lambda *a: pytest.fail("создание задачи, не комментарий"))

    result = sd.detect_and_act(REPO, NOW, [REAL_PIPELINE_PAUSED], run_url="https://github.com/x/actions/runs/1")
    assert any("#999" in line and "заведена автодетектором" in line for line in result)
    # тело задачи ушло через gh — проверяем улику и отпечаток в вызове POST
    create_call = next(c for c in fake.calls if "POST" in c)
    assert "gate:pipeline-paused" in create_call
    assert REAL_PIPELINE_PAUSED in create_call


# ── Суточный потолок ────────────────────────────────────────────────────────

def test_daily_cap_blocks_creation_loudly_once_exhausted(monkeypatch):
    first_seen = (NOW - timedelta(minutes=sd.STALL_PERSIST_MINUTES + 5)).isoformat().replace("+00:00", "Z")
    already_created = [
        _issue(100 + i, f"Отпечаток: `check:red:whatever-{i}`",
               created_at=(NOW - timedelta(hours=1)).isoformat().replace("+00:00", "Z"))
        for i in range(sd.STALL_DAILY_CAP)
    ]
    fake = FakeGh({
        "issues?state=open&labels=auto-detected": [],
        "issues/120/comments": [
            {"created_at": first_seen, "body": f"👀 {sd._sighting_marker('gate:pipeline-paused')}\n..."},
        ],
        "issues?state=all&labels=auto-detected": already_created,
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sd, "post_issue_comment", lambda *a: None)

    def fail_create(*a, **k):
        pytest.fail("потолок исчерпан — создание задачи запрещено")
    monkeypatch.setattr(sd, "create_task", fail_create)

    result = sd.detect_and_act(REPO, NOW, [REAL_PIPELINE_PAUSED])
    assert any("потолок" in line and "исчерпан" in line for line in result)


# ── Холостой ход: здоровый конвейер — ни одного вызова ─────────────────────

def test_idle_conveyor_makes_zero_calls(monkeypatch):
    """Гвардия #201: здоровый пульс не читает и не пишет НИЧЕГО через gh.
    Мутация: закомментируй `if not signals: return []` в detect_and_act —
    этот тест покраснеет первым (FakeGh.calls перестанет быть пустым)."""
    fake = FakeGh({})  # ни одного маршрута — любой вызов роняет тест
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sd, "post_issue_comment", lambda *a: pytest.fail("здоровый пульс не пишет"))
    monkeypatch.setattr(pg, "send_telegram", lambda *a: pytest.fail("здоровый пульс не шлёт Telegram"))

    healthy_report = [
        "## Отчёт оркестратора 2026-09-03T12:00:00+00:00",
        "Открытых PR: 2",
        "Пул задач: 1 свободно, 1 в работе",
        "Действий не требуется.",
    ]
    result = sd.detect_and_act(REPO, NOW, healthy_report)
    assert result == []
    assert fake.calls == []


def test_idle_escalation_when_no_auto_tasks_makes_no_mutating_call(monkeypatch):
    fake = FakeGh({"issues?state=open&labels=auto-detected": []})
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sd, "escalate", lambda *a: pytest.fail("эскалировать нечего"))
    result = sd.escalate_stale_auto_tasks(REPO, NOW)
    assert result == []
    # единственный вызов — чтение списка автозадач, ни одного изменяющего
    assert all("-X" not in c for c in fake.calls)


# ── Эскалация затянувшейся автозадачи ──────────────────────────────────────

def test_escalate_stale_auto_task_once(monkeypatch):
    old = _issue(555, "Отпечаток: `check:red:test`",
                 created_at=(NOW - timedelta(hours=sd.ESCALATE_AFTER_HOURS + 1)).isoformat().replace("+00:00", "Z"))
    fake = FakeGh({
        "issues?state=open&labels=auto-detected": [old],
        "issues/555/comments": [],  # ещё не эскалирована
    })
    patch_gh(monkeypatch, fake)
    calls = []
    monkeypatch.setattr(sd, "escalate", lambda repo, n, text: calls.append((n, text)) or "ok")

    result = sd.escalate_stale_auto_tasks(REPO, NOW)
    assert len(calls) == 1 and calls[0][0] == 555
    assert "что дальше" in calls[0][1].lower()
    assert any("#555" in line for line in result)


def test_escalate_stale_auto_task_not_repeated(monkeypatch):
    old = _issue(555, "Отпечаток: `check:red:test`",
                 created_at=(NOW - timedelta(hours=sd.ESCALATE_AFTER_HOURS + 1)).isoformat().replace("+00:00", "Z"))
    fake = FakeGh({
        "issues?state=open&labels=auto-detected": [old],
        "issues/555/comments": [
            {"created_at": "2026-09-02T00:00:00Z", "body": f"x {sd.ESCALATION_MARKER}"},
        ],
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sd, "escalate", lambda *a: pytest.fail("уже эскалирована — второй раз не нужно"))

    result = sd.escalate_stale_auto_tasks(REPO, NOW)
    assert result == []
