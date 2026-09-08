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
    fake = FakeGh({
        "issues?state=open&labels=auto-detected": [existing],
        "issues/300/comments": [],  # эта улика ещё не комментировалась
    })
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


def test_detect_and_act_same_evidence_twice_posts_one_comment(monkeypatch):
    """Находка AI-ревью PR #248: хронический простой (та же улика на каждом
    пульсе) не должен плодить комментарий-дубликат — до 96/сутки при
    интервале 15 мин, и настоящая новая улика тонет в потоке повторов.
    Первый вызов detect_and_act пишет комментарий с маркером улики; второй
    вызов с ТОЙ ЖЕ уликой находит свой же маркер в списке комментариев
    (эмулируется добавлением его в фикстуру между вызовами) и молчит."""
    existing = _issue(300, "тело\n\nОтпечаток: `gate:pipeline-paused`\n\nостальное")
    comments: list[dict] = []
    fake = FakeGh({
        "issues?state=open&labels=auto-detected": [existing],
        "issues/300/comments": comments,
    })
    patch_gh(monkeypatch, fake)
    posted = []

    def record(repo, n, text):
        posted.append((n, text))
        comments.append({"created_at": "2026-09-03T11:00:00Z", "body": text})
    monkeypatch.setattr(sd, "post_issue_comment", record)
    monkeypatch.setattr(sd, "create_task", lambda *a, **k: pytest.fail("дубликат по отпечатку"))

    result_1 = sd.detect_and_act(REPO, NOW, [REAL_PIPELINE_PAUSED])
    result_2 = sd.detect_and_act(REPO, NOW, [REAL_PIPELINE_PAUSED])  # та же улика опять

    assert len(posted) == 1  # ровно один комментарий на два подряд одинаковых пульса
    assert any("новая улика" in line for line in result_1)
    assert any("не изменилась" in line for line in result_2)


def test_detect_and_act_changed_evidence_posts_second_comment(monkeypatch):
    """Зеркало предыдущего теста: улика ИЗМЕНИЛАСЬ (другой run_url в строке
    отчёта того же отпечатка) — второй комментарий обязан уйти, дедупликация
    не должна глушить настоящую новую информацию."""
    existing = _issue(300, "тело\n\nОтпечаток: `gate:pipeline-paused`\n\nостальное")
    comments: list[dict] = []
    fake = FakeGh({
        "issues?state=open&labels=auto-detected": [existing],
        "issues/300/comments": comments,
    })
    patch_gh(monkeypatch, fake)
    posted = []

    def record(repo, n, text):
        posted.append((n, text))
        comments.append({"created_at": "2026-09-03T11:00:00Z", "body": text})
    monkeypatch.setattr(sd, "post_issue_comment", record)
    monkeypatch.setattr(sd, "create_task", lambda *a, **k: pytest.fail("дубликат по отпечатку"))

    other_pause_line = (
        "🚨 конвейер на паузе: 4 красных прогонов worker.yml подряд — "
        "диспатч остановлен (уже оповещено, см. #120)"
    )
    sd.detect_and_act(REPO, NOW, [REAL_PIPELINE_PAUSED])
    sd.detect_and_act(REPO, NOW, [other_pause_line])  # тот же отпечаток, другая улика

    assert len(posted) == 2


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
        "issues?state=closed&labels=auto-detected": [],  # прежних решённых задач по отпечатку нет
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
        "issues?state=closed&labels=auto-detected": [],  # прежних решённых задач по отпечатку нет
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


# ── Сброс устойчивости после закрытия прошлой автозадачи ───────────────────

def test_blip_after_task_closed_does_not_bypass_persistence(monkeypatch):
    """Мутационная гвардия находки AI-ревью PR #248 (обход предохранителя
    устойчивости, воспроизведён прогоном): маркер первого наблюдения в
    WATCHDOG_ISSUE многодневной давности, но задача по этому отпечатку УЖЕ
    закрыта 5 минут назад — старая улика не в счёт нового эпизода. Один
    блип отпечатка ПОСЛЕ закрытия обязан считаться первым наблюдением
    нового эпизода (жди STALL_PERSIST_MINUTES заново), а не мгновенно
    заводить вторую задачу. Снятие фильтра по `_closed_task_reset_times` в
    detect_and_act красит этот тест — `create_task` вызывается."""
    old_marker_time = NOW - timedelta(days=8)
    task_closed_at = (NOW - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    closed_task = {
        "number": 300,
        "body": "тело\n\nОтпечаток: `gate:pipeline-paused`\n\nостальное",
        "closed_at": task_closed_at,
        "labels": [{"name": "task"}, {"name": "auto-detected"}],
    }
    fake = FakeGh({
        "issues?state=open&labels=auto-detected": [],  # старая задача уже закрыта
        "issues?state=closed&labels=auto-detected": [closed_task],
        "issues/120/comments": [
            {"created_at": old_marker_time.isoformat().replace("+00:00", "Z"),
             "body": f"👀 {sd._sighting_marker('gate:pipeline-paused')}\n..."},
        ],
    })
    patch_gh(monkeypatch, fake)
    posted = []
    monkeypatch.setattr(sd, "post_issue_comment", lambda repo, n, text: posted.append((n, text)))

    def fail_create(*a, **k):
        pytest.fail("старый маркер до закрытия предыдущей задачи не считается устойчивостью нового эпизода")
    monkeypatch.setattr(sd, "create_task", fail_create)

    result = sd.detect_and_act(REPO, NOW, [REAL_PIPELINE_PAUSED])
    assert len(posted) == 1  # новый маркер первого наблюдения нового эпизода
    assert posted[0][0] == sd.WATCHDOG_ISSUE
    assert sd._sighting_marker("gate:pipeline-paused") in posted[0][1]
    assert any("замечен впервые" in line for line in result)


def test_marker_after_reset_boundary_creates_task_once_persisted(monkeypatch):
    """Зеркало предыдущего теста: сайтинг ПОСЛЕ закрытия прошлой задачи,
    сам по себе продержавшийся дольше порога, обязан завести задачу как
    обычно — фильтр по границе сброса не глушит новый эпизод целиком."""
    task_closed_at = (NOW - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    new_episode_seen = NOW - timedelta(minutes=sd.STALL_PERSIST_MINUTES + 5)
    closed_task = {
        "number": 300,
        "body": "тело\n\nОтпечаток: `gate:pipeline-paused`\n\nостальное",
        "closed_at": task_closed_at,
        "labels": [{"name": "task"}, {"name": "auto-detected"}],
    }
    fake = FakeGh({
        "issues?state=open&labels=auto-detected": [],
        "issues?state=closed&labels=auto-detected": [closed_task],
        "issues/120/comments": [
            {"created_at": new_episode_seen.isoformat().replace("+00:00", "Z"),
             "body": f"👀 {sd._sighting_marker('gate:pipeline-paused')}\n..."},
        ],
        "issues?state=all&labels=auto-detected": [],
        "POST repos/mytab0r/edge-harness/issues": {"number": 999},
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sd, "post_issue_comment", lambda *a: pytest.fail("сайтинг новее границы сброса — не первое наблюдение"))

    result = sd.detect_and_act(REPO, NOW, [REAL_PIPELINE_PAUSED])
    assert any("#999" in line and "заведена автодетектором" in line for line in result)


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
        "issues?state=closed&labels=auto-detected": [],  # прежних решённых задач по отпечатку нет
        "issues/120/comments": [
            {"created_at": first_seen, "body": f"👀 {sd._sighting_marker('gate:pipeline-paused')}\n..."},
        ],
        "issues?state=all&labels=auto-detected": already_created,
        # gate:pipeline-paused — не check:red:*, требуемые контексты тут ни при чём,
        # но код всё равно читает защиту ветки лениво на пути исчерпания потолка (#637).
        "branches/main/protection": {"required_status_checks": {"contexts": ["test", "contract"]}},
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sd, "post_issue_comment", lambda *a: None)
    escalated = []
    monkeypatch.setattr(
        sd, "escalate",
        lambda repo, n, text: escalated.append((n, text)) or "Telegram: доставлен; след в #120: оставлен",
    )

    def fail_create(*a, **k):
        pytest.fail("потолок исчерпан — создание задачи запрещено")
    monkeypatch.setattr(sd, "create_task", fail_create)

    result = sd.detect_and_act(REPO, NOW, [REAL_PIPELINE_PAUSED])
    assert any("потолок" in line and "исчерпан" in line for line in result)
    # #610/#637: исчерпание потолка обязано уйти активным каналом, не только в report
    assert len(escalated) == 1
    assert escalated[0][0] == sd.WATCHDOG_ISSUE
    assert "gate:pipeline-paused" in escalated[0][1]
    assert any("эскалация потолка автозаведения" in line for line in result)


def test_daily_cap_stops_creation_mid_pulse_across_many_new_fingerprints(monkeypatch):
    """Находка AI-ревью PR #248 (второй раунд): счётчик `created_today` растёт
    ВНУТРИ одного вызова `detect_and_act` (одна строка отчёта может нести
    несколько новых отпечатков разом — «красные проверки: a, b, c, ...»), а
    прежний тест кормил детектор только ОДНИМ отпечатком при уже заполненной
    истории — накопление счётчика в одном пульсе не было доказано ничем.
    Мутация: убери `created_today += 1` после `create_task` в detect_and_act —
    этот тест покраснеет (create_task вызовется больше STALL_DAILY_CAP раз)."""
    names = [f"fresh-{i}" for i in range(sd.STALL_DAILY_CAP + 2)]  # 7 новых отпечатков разом
    first_seen = (NOW - timedelta(minutes=sd.STALL_PERSIST_MINUTES + 5)).isoformat().replace("+00:00", "Z")
    fake = FakeGh({
        "issues?state=open&labels=auto-detected": [],
        "issues?state=closed&labels=auto-detected": [],
        "issues/120/comments": [
            {"created_at": first_seen, "body": f"👀 {sd._sighting_marker(f'check:red:{name}')}\n..."}
            for name in names
        ],
        "issues?state=all&labels=auto-detected": [],  # created_today стартует с нуля
        # ни один "fresh-i" не required-контекст — все сверх потолка остаются капнутыми (#637).
        "branches/main/protection": {"required_status_checks": {"contexts": ["test", "contract"]}},
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sd, "post_issue_comment", lambda *a: None)
    monkeypatch.setattr(sd, "escalate", lambda repo, n, text: "ok")

    created = []
    def counting_create(repo, fingerprint, evidence, run_url):
        created.append(fingerprint)
        return 1000 + len(created)
    monkeypatch.setattr(sd, "create_task", counting_create)

    report_line = "⏸️ #205 — красные проверки: " + ", ".join(names)
    result = sd.detect_and_act(REPO, NOW, [report_line])

    assert len(created) == sd.STALL_DAILY_CAP
    capped = [line for line in result if "НЕ заведён" in line]
    assert len(capped) == 2  # ровно два отпечатка сверх потолка не заведены


# ── Исключение из потолка для блокирующего класса (#637) ────────────────────

def test_is_blocking_fingerprint_matches_only_required_context():
    required = {"test", "contract"}
    assert sd._is_blocking_fingerprint("check:red:test", required) is True
    assert sd._is_blocking_fingerprint("check:red:contract", required) is True
    assert sd._is_blocking_fingerprint("check:red:forge", required) is False  # не required (issue #120, 2026-09-06)
    assert sd._is_blocking_fingerprint("gate:pipeline-paused", required) is False
    assert sd._is_blocking_fingerprint("warn:что-то", required) is False


def test_daily_cap_does_not_block_required_check_red_class(monkeypatch):
    """Мутация (докажи по AGENTS.md «Починил случай — закрой класс»): убери
    ветку `_is_blocking_fingerprint` в detect_and_act — этот тест покраснеет
    (`create_task` не вызовется, потолок проглотит требуемую проверку).
    Прод-форма: `gh api repos/mytab0r/edge-harness/branches/main/protection`
    (снято 2026-09-07) — required_status_checks.contexts = ["test","contract"]."""
    first_seen = (NOW - timedelta(minutes=sd.STALL_PERSIST_MINUTES + 5)).isoformat().replace("+00:00", "Z")
    already_created = [
        _issue(100 + i, f"Отпечаток: `check:red:whatever-{i}`",
               created_at=(NOW - timedelta(hours=1)).isoformat().replace("+00:00", "Z"))
        for i in range(sd.STALL_DAILY_CAP)
    ]
    fake = FakeGh({
        "issues?state=open&labels=auto-detected": [],
        "issues?state=closed&labels=auto-detected": [],
        "issues/120/comments": [
            {"created_at": first_seen, "body": f"👀 {sd._sighting_marker('check:red:test')}\n..."},
        ],
        "issues?state=all&labels=auto-detected": already_created,  # потолок уже 5/5
        # снято реальным gh api repos/mytab0r/edge-harness/branches/main/protection (2026-09-07)
        "branches/main/protection": {
            "required_status_checks": {"strict": True, "contexts": ["test", "contract"]},
        },
        "POST repos/mytab0r/edge-harness/issues": {"number": 999},
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sd, "post_issue_comment", lambda *a: None)
    monkeypatch.setattr(sd, "escalate", lambda repo, n, text: "ok")

    result = sd.detect_and_act(REPO, NOW, ["⏸️ #205 — красные проверки: test"])

    assert any("#999" in line and "заведена автодетектором" in line for line in result)
    assert any("исключение из потолка" in line for line in result)
    create_call = next(c for c in fake.calls if "POST" in c)
    assert "check:red:test" in create_call


def test_daily_cap_still_blocks_non_required_check_when_exhausted(monkeypatch):
    """Зеркало предыдущего теста: рядовой (не required) отпечаток по-прежнему
    НЕ заводится при исчерпанном потолке — исключение не открывает дорогу
    спаму. `check:red:forge` — живой отпечаток issue #120 (2026-09-06),
    `forge` не входит в required_status_checks этого репозитория."""
    first_seen = (NOW - timedelta(minutes=sd.STALL_PERSIST_MINUTES + 5)).isoformat().replace("+00:00", "Z")
    already_created = [
        _issue(100 + i, f"Отпечаток: `check:red:whatever-{i}`",
               created_at=(NOW - timedelta(hours=1)).isoformat().replace("+00:00", "Z"))
        for i in range(sd.STALL_DAILY_CAP)
    ]
    fake = FakeGh({
        "issues?state=open&labels=auto-detected": [],
        "issues?state=closed&labels=auto-detected": [],
        "issues/120/comments": [
            {"created_at": first_seen, "body": f"👀 {sd._sighting_marker('check:red:forge')}\n..."},
        ],
        "issues?state=all&labels=auto-detected": already_created,
        "branches/main/protection": {"required_status_checks": {"contexts": ["test", "contract"]}},
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sd, "post_issue_comment", lambda *a: None)
    monkeypatch.setattr(sd, "escalate", lambda repo, n, text: "ok")

    def fail_create(*a, **k):
        pytest.fail("forge не required — исключение из потолка не должно сработать")
    monkeypatch.setattr(sd, "create_task", fail_create)

    result = sd.detect_and_act(REPO, NOW, ["⏸️ #205 — красные проверки: forge"])
    assert any("потолок" in line and "исчерпан" in line for line in result)


# ── Эскалация исчерпания потолка держится на факте ДОСТАВКИ, не попытки
#    (#610 — видимость эскалации; находка AI-ревью PR #656/#637 — дедуп по
#    доставке, не по факту issue-комментария, который escalate() пишет
#    безусловно) ──────────────────────────────────────────────────────────

def test_cap_exhausted_escalates_visibly_without_raising(monkeypatch):
    # Мутационная проверка #610 (инвариант «наблюдатель провалов не реагирует
    # на свою инфраструктуру мониторинга»): исчерпание потолка обязано дать
    # ВИДИМЫЙ сигнал (комментарий в #120 с CAP_EXHAUSTED_MARKER + Telegram),
    # но НЕ бросить исключение — main() красит прогон только по RuntimeError
    # из detect_and_act (stall_hard_failure), не по бизнес-исходу «потолок».
    first_seen = (NOW - timedelta(minutes=sd.STALL_PERSIST_MINUTES + 5)).isoformat().replace("+00:00", "Z")
    already_created = [
        _issue(100 + i, f"Отпечаток: `check:red:whatever-{i}`",
               created_at=(NOW - timedelta(hours=1)).isoformat().replace("+00:00", "Z"))
        for i in range(sd.STALL_DAILY_CAP)
    ]
    fake = FakeGh({
        "issues?state=open&labels=auto-detected": [],
        "issues?state=closed&labels=auto-detected": [],
        "issues/120/comments": [
            {"created_at": first_seen, "body": f"👀 {sd._sighting_marker('gate:pipeline-paused')}\n..."},
        ],
        "issues?state=all&labels=auto-detected": already_created,
        # gate:pipeline-paused — не check:red:*, требуемые контексты тут ни при чём,
        # но код всё равно читает защиту ветки лениво на пути исчерпания потолка (#637).
        "branches/main/protection": {"required_status_checks": {"contexts": ["test", "contract"]}},
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sd, "post_issue_comment", lambda *a: None)
    posted = []
    monkeypatch.setattr(pg, "post_issue_comment", lambda repo, n, text: posted.append((n, text)))
    monkeypatch.setattr(pg, "send_telegram", lambda *a, **k: True)
    monkeypatch.setattr(sd, "create_task", lambda *a, **k: pytest.fail("потолок исчерпан — создание запрещено"))

    result = sd.detect_and_act(REPO, NOW, [REAL_PIPELINE_PAUSED])  # не бросает исключение

    assert any("потолок" in line and "исчерпан" in line for line in result)
    assert any("эскалация потолка" in line for line in result)
    # Telegram доставлен (send_telegram → True) — эскалация запись в #120
    # (posted[0] — сам алерт, изнутри escalate()).
    assert len(posted) == 1 and posted[0][0] == sd.WATCHDOG_ISSUE
    assert sd.CAP_EXHAUSTED_MARKER in posted[0][1]
    assert NOW.date().isoformat() in posted[0][1]


def test_cap_exhausted_escalation_deduped_once_per_day(monkeypatch):
    # Второй пульс того же дня, потолок всё ещё исчерпан, и ДОСТАВКА уже
    # ПОДТВЕРЖДЕНА (CAP_EXHAUSTED_DELIVERED_MARKER стоит в #120) — повторной
    # эскалации/Telegram не шлём (не спамим весь день на один и тот же
    # исчерпанный потолок).
    first_seen = (NOW - timedelta(minutes=sd.STALL_PERSIST_MINUTES + 5)).isoformat().replace("+00:00", "Z")
    already_created = [
        _issue(100 + i, f"Отпечаток: `check:red:whatever-{i}`",
               created_at=(NOW - timedelta(hours=1)).isoformat().replace("+00:00", "Z"))
        for i in range(sd.STALL_DAILY_CAP)
    ]
    delivered_marker = f"{sd.CAP_EXHAUSTED_DELIVERED_MARKER} {NOW.date().isoformat()}]"
    fake = FakeGh({
        "issues?state=open&labels=auto-detected": [],
        "issues?state=closed&labels=auto-detected": [],
        "issues/120/comments": [
            {"created_at": first_seen, "body": f"👀 {sd._sighting_marker('gate:pipeline-paused')}\n..."},
            {"created_at": NOW.isoformat().replace("+00:00", "Z"), "body": f"{delivered_marker}\nПодтверждение доставки: ..."},
        ],
        "issues?state=all&labels=auto-detected": already_created,
        "branches/main/protection": {"required_status_checks": {"contexts": ["test", "contract"]}},
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sd, "post_issue_comment", lambda *a: pytest.fail("доставка уже подтверждена сегодня — второй раз не пишем"))
    monkeypatch.setattr(pg, "post_issue_comment", lambda *a: pytest.fail("эскалация уже была сегодня — второй раз не пишем"))
    monkeypatch.setattr(pg, "send_telegram", lambda *a, **k: pytest.fail("эскалация уже была сегодня — Telegram не шлём"))
    monkeypatch.setattr(sd, "create_task", lambda *a, **k: pytest.fail("потолок исчерпан — создание запрещено"))

    result = sd.detect_and_act(REPO, NOW, [REAL_PIPELINE_PAUSED])
    assert any("потолок" in line and "исчерпан" in line for line in result)
    assert not any("эскалация потолка" in line for line in result)


def test_cap_exhausted_escalation_not_delivered_is_not_deduped_same_day(monkeypatch):
    """Находка AI-ревью PR #656: escalate() пишет issue-комментарий с
    CAP_EXHAUSTED_MARKER БЕЗУСЛОВНО, до попытки Telegram — если бы дедуп
    держался на этом комментарии (а не на CAP_EXHAUSTED_DELIVERED_MARKER),
    недоставленный Telegram молча признавался бы «сигнализированным» на весь
    остаток суток. Здесь Telegram НЕ доставляет (send_telegram → False) —
    подтверждающий маркер не появляется, и следующий пульс (тот же
    календарный день) повторяет попытку, а не молчит до завтра.

    Честно про докстринг предыдущей версии этого теста (проверено мутацией
    руками, 2026-09-08): заявленная мутация `"Telegram: доставлен" in result`
    → `True` НЕ красила тест только через `telegram_calls`/подстроку «НЕ
    доставлен» — обе стороны `if` кладут в report СТРОКУ САМОГО `result`
    (возврата `escalate()`), а `result` содержит «Telegram: НЕ доставлен»
    независимо от того, какая ветка сработала (send_telegram в фикстуре
    всё равно возвращает False) — подстрока совпадает в обоих случаях, и
    telegram_calls растёт на пульс в обоих случаях тоже (второй пульс видит
    в FakeGh тот же статичный список комментариев, раз sd.post_issue_comment
    — no-op и ничего в него не пишет). Настоящая наблюдаемая разница мутации
    — ПОДТВЕРЖДАЮЩИЙ маркер (CAP_EXHAUSTED_DELIVERED_MARKER) пишется, хотя
    доставки не было; confirm_calls ниже — прямое наблюдение за этим фактом.
    Мутация: замени `"Telegram: доставлен" in result` на `True` в
    detect_and_act — confirm_calls перестанет быть пустым (доказано этим же
    прогоном ниже, снятием фикса)."""
    first_seen = (NOW - timedelta(minutes=sd.STALL_PERSIST_MINUTES + 5)).isoformat().replace("+00:00", "Z")
    already_created = [
        _issue(100 + i, f"Отпечаток: `check:red:whatever-{i}`",
               created_at=(NOW - timedelta(hours=1)).isoformat().replace("+00:00", "Z"))
        for i in range(sd.STALL_DAILY_CAP)
    ]
    fake = FakeGh({
        "issues?state=open&labels=auto-detected": [],
        "issues?state=closed&labels=auto-detected": [],
        "issues/120/comments": [
            {"created_at": first_seen, "body": f"👀 {sd._sighting_marker('gate:pipeline-paused')}\n..."},
        ],
        "issues?state=all&labels=auto-detected": already_created,
        "branches/main/protection": {"required_status_checks": {"contexts": ["test", "contract"]}},
    })
    patch_gh(monkeypatch, fake)
    confirm_calls = []
    monkeypatch.setattr(sd, "post_issue_comment", lambda *a: confirm_calls.append(a))
    monkeypatch.setattr(pg, "post_issue_comment", lambda *a: None)  # сам алерт escalate() пишет best-effort
    telegram_calls = []
    monkeypatch.setattr(pg, "send_telegram", lambda *a, **k: telegram_calls.append(1) or False)
    monkeypatch.setattr(sd, "create_task", lambda *a, **k: pytest.fail("потолок исчерпан — создание запрещено"))

    result = sd.detect_and_act(REPO, NOW, [REAL_PIPELINE_PAUSED])
    assert any("НЕ доставлен" in line for line in result)
    assert len(telegram_calls) == 1
    # Доставки не было — подтверждающий маркер НЕ пишется (единственная
    # наблюдаемая гарантия, из которой следует «следующий пульс повторит»).
    assert confirm_calls == []

    # Тот же календарный день, следующий пульс (~15 мин) — повтор, не
    # молчание до завтра: подтверждающего маркера так и не появилось.
    result_2 = sd.detect_and_act(REPO, NOW + timedelta(minutes=15), [REAL_PIPELINE_PAUSED])
    assert any("НЕ доставлен" in line for line in result_2)
    assert len(telegram_calls) == 2
    assert confirm_calls == []


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
