#!/usr/bin/env python3
"""Тесты producer_orphan_watch.py (issue #1277) — поведенческие, на
прод-форме данных.

Фикстуры `fixtures_producer_orphan_watch_1277.json` — НЕ пересказ формата:
это реальные issues пула, снятые `gh api graphql` с mytab0r/edge-harness
2026-09-15 (see issue #1277 body/PR для полной команды), урезанные до
number/title/state/created_at/closed_at/body/labels/assignees — тех же
полей, что читает `compute_stats`. `tail_12` — первые 12 (по created_at)
реальных «Хвост чеклиста ревью PR #N» (мёртвый производитель, живая
улика); `ci_failure_15` — первые 15 реальных failure-watch issues (здоровый
производитель, 94% закрытия по полной истории); `dependabot_1` —
единственный реальный dependabot-alert-watch issue на дату замера (молодой
производитель, N=1).

Мутация, которой доказана проверка (класс #1194, AGENTS.md «докажи
мутацией — исполни, не вспоминай»): закомментируй ветку `if close_rate <=
dead_close_rate_max: ... return check_result.violation(...)` в
`evaluate_producer` (`scripts/lib/producer_orphan_watch.py`) — замени на
безусловный `return check_result.ok()` — `test_dead_producer_checklist_tail_fires`,
`test_evaluate_all_returns_per_producer_check_results` и
`test_threshold_min_sample_size_gates_the_verdict` краснеют (реальный
мёртвый производитель перестаёт находиться), верни ветку обратно. Прогон
до/после исполнен при подготовке PR, реализующего issue #1277 — вывод
`pytest` до мутации (3 failed) и после возврата (15 passed) приведён в
теле PR дословно.

Запуск: python -m pytest scripts/lib/test_producer_orphan_watch.py -q
"""

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

_DIR = Path(__file__).resolve().parent

_POW_SPEC = importlib.util.spec_from_file_location(
    "producer_orphan_watch", _DIR / "producer_orphan_watch.py")
producer_orphan_watch = importlib.util.module_from_spec(_POW_SPEC)
_POW_SPEC.loader.exec_module(producer_orphan_watch)  # type: ignore[union-attr]

_PI_SPEC = importlib.util.spec_from_file_location("pool_issue", _DIR / "pool_issue.py")
pool_issue = importlib.util.module_from_spec(_PI_SPEC)
_PI_SPEC.loader.exec_module(pool_issue)  # type: ignore[union-attr]

_CR_SPEC = importlib.util.spec_from_file_location("check_result", _DIR / "check_result.py")
check_result = importlib.util.module_from_spec(_CR_SPEC)
_CR_SPEC.loader.exec_module(check_result)  # type: ignore[union-attr]

with open(_DIR / "fixtures_producer_orphan_watch_1277.json", encoding="utf-8") as _f:
    FIXTURES = json.load(_f)

TAIL_12 = FIXTURES["tail_12"]
CI_FAILURE_15 = FIXTURES["ci_failure_15"]
DEPENDABOT_1 = FIXTURES["dependabot_1"]

# «Сейчас» для тестов — чуть позже самой свежей реальной issue датасета
# (2026-09-14T20:52:07Z), фиксировано, не datetime.now(): тест обязан быть
# воспроизводим завтра тем же результатом.
NOW = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)


# ── classify_producer: маркер против legacy ─────────────────────────────────

def test_classify_producer_prefers_marker_over_everything():
    """Issue с телом, несущим маркер производителя (issue #1277, «Требование
    1»), классифицируется по маркеру ДАЖЕ если заголовок/метки предполагают
    другого производителя — маркер не переопределяем задним числом."""
    body = pool_issue.producer_marker("upstream-drift") + "\nтело задачи"
    issue = {
        "title": "Хвост чеклиста ревью PR #999",  # выглядит как checklist-tail
        "state": "open",
        "created_at": "2026-09-15T00:00:00Z",
        "body": body,
        "labels": [{"name": "task"}],
        "assignees": [],
    }
    assert producer_orphan_watch.classify_producer(issue) == "upstream-drift"


def test_classify_producer_legacy_checklist_tail_real_fixture():
    issue = TAIL_12[0]
    assert pool_issue.extract_producer(issue["body"]) is None  # реальная issue без маркера
    assert producer_orphan_watch.classify_producer(issue) == "checklist-tail"


def test_classify_producer_legacy_failure_watch_real_fixture():
    for issue in CI_FAILURE_15:
        assert producer_orphan_watch.classify_producer(issue) == "failure-watch"


def test_classify_producer_legacy_dependabot_real_fixture():
    issue = DEPENDABOT_1[0]
    assert producer_orphan_watch.classify_producer(issue) == "dependabot-alert-watch"


def test_classify_producer_review_findings_is_unrecognizable_by_legacy_path():
    """Требование 1, честная граница: review-findings (file_tasks.py) не
    несёт ни маркера (issue заведена до #1277), ни отличительной метки/
    заголовка — заголовок и тело пишет модель ревью, произвольны по
    конструкции. Прод-форма реальной такой issue (пула, до маркера):
    ТОЛЬКО `labels=["task"]`, заголовок — свободный текст находки."""
    issue = {
        "number": 777,
        "title": "declared_deps.py: поле «Чем блокируется» не парсит несколько номеров через запятую",
        "state": "open",
        "created_at": "2026-09-10T00:00:00Z",
        "closed_at": None,
        "body": "## Цель\n...\n## Критерий готовности\n...",
        "labels": [{"name": "task"}],
        "assignees": [],
    }
    assert producer_orphan_watch.classify_producer(issue) is None


# ── compute_stats на реальных фикстурах ─────────────────────────────────────

def test_compute_stats_includes_all_nine_producers_even_with_zero_issues():
    stats = producer_orphan_watch.compute_stats(TAIL_12, NOW)
    assert set(stats) == set(producer_orphan_watch.PRODUCER_IDS)
    # health-audit/merge-health-watch ни разу не встретились в TAIL_12 —
    # обязаны остаться в результате с total=0, не выпасть из словаря.
    assert stats["health-audit"].total == 0
    assert stats["merge-health-watch"].total == 0


def test_compute_stats_checklist_tail_matches_live_measurement_shape():
    stats = producer_orphan_watch.compute_stats(TAIL_12, NOW)
    tail = stats["checklist-tail"]
    assert tail.total == 12
    assert tail.closed == 0
    assert tail.open_count == 12
    assert tail.ever_taken == 0
    # Старейшая issue датасета — 2026-09-06T21:39:48Z, NOW — 2026-09-15T12:00Z:
    # больше 8 суток, безусловно старше DEFAULT_MIN_OLDEST_OPEN_HOURS (24ч).
    assert tail.oldest_open_hours > 24


def test_compute_stats_failure_watch_matches_live_measurement_shape():
    stats = producer_orphan_watch.compute_stats(CI_FAILURE_15, NOW)
    fw = stats["failure-watch"]
    assert fw.total == 15
    # Живой замер (issue #1277): 94% закрытия на полной истории (32 issue);
    # подвыборка первых 15 (по времени создания, более старые — больше
    # шансов быть закрытыми) не обязана давать РОВНО ту же долю, но обязана
    # остаться высокой (санитарная проверка, что фикстура не вырождена).
    assert fw.closed / fw.total >= 0.5


# ── evaluate_producer: три исхода ────────────────────────────────────────────

def test_dead_producer_checklist_tail_fires():
    """Живая улика issue #1277: производитель с достаточным объёмом (>=10),
    долей закрытия на/ниже порога и достаточно старой открытой задачей —
    ВСЕГДА violation(). Это ЦЕЛЬ замера — если этот тест не падает после
    снятия проверки в evaluate_producer (см. докстринг модуля, «Мутация»),
    проверка не работает."""
    stats = producer_orphan_watch.compute_stats(TAIL_12, NOW)
    result = producer_orphan_watch.evaluate_producer(stats["checklist-tail"])
    assert result.status == check_result.STATUS_VIOLATION
    assert result.violations[0]["producer"] == "checklist-tail"
    assert result.violations[0]["close_rate"] == 0.0
    assert result.violations[0]["total"] == 12


def test_healthy_producer_failure_watch_is_silent():
    stats = producer_orphan_watch.compute_stats(CI_FAILURE_15, NOW)
    result = producer_orphan_watch.evaluate_producer(stats["failure-watch"])
    assert result.status == check_result.STATUS_OK
    assert result.violations == []


def test_young_producer_dependabot_is_third_state_not_ok_not_violation():
    """Требование 3: N=1 (реальный живой случай) — недостаточно данных,
    НЕ ok() (не индульгенция молчанием) и НЕ violation() (не обвинение по
    единственному наблюдению)."""
    stats = producer_orphan_watch.compute_stats(DEPENDABOT_1, NOW)
    result = producer_orphan_watch.evaluate_producer(stats["dependabot-alert-watch"])
    assert result.status == check_result.STATUS_UNKNOWN
    assert "dependabot-alert-watch" in result.reason
    assert "1" in result.reason  # объём назван фактом, не спрятан


def test_never_fired_producer_is_third_state():
    """health-audit/merge-health-watch на дату внедрения (issue #1277) ни
    разу не завели ни одной задачи (total=0) — тот же третий исход, не
    ложный ok() («нет данных о нарушениях» != «производитель здоров»)."""
    stats = producer_orphan_watch.compute_stats(TAIL_12, NOW)  # ни один из 12 — не health-audit
    result = producer_orphan_watch.evaluate_producer(stats["health-audit"])
    assert result.status == check_result.STATUS_UNKNOWN
    assert "0 задач" in result.reason


def test_recent_zero_close_rate_is_third_state_not_immediate_violation():
    """Требование 3, вторая ветка: доля закрытия ноль, объём выше порога, но
    единственная старейшая открытая задача моложе порога свежести — рано
    считать застоем (ей физически не могли ещё дать взять)."""
    fresh_created = "2026-09-15T11:00:00Z"  # час до NOW — моложе 24ч
    issues = [
        {
            "title": f"Простой конвейера: класс-{i}",
            "state": "open",
            "created_at": fresh_created,
            "body": "тело",
            "labels": [{"name": "task"}, {"name": "auto-detected"}],
            "assignees": [],
        }
        for i in range(11)
    ]
    stats = producer_orphan_watch.compute_stats(issues, NOW)
    result = producer_orphan_watch.evaluate_producer(stats["stall-detector"])
    assert result.status == check_result.STATUS_UNKNOWN
    assert "рано считать застоем" in result.reason


def test_evaluate_all_returns_per_producer_check_results():
    stats = producer_orphan_watch.compute_stats(TAIL_12 + CI_FAILURE_15 + DEPENDABOT_1, NOW)
    results = producer_orphan_watch.evaluate_all(stats)
    assert results["checklist-tail"].status == check_result.STATUS_VIOLATION
    assert results["failure-watch"].status == check_result.STATUS_OK
    assert results["dependabot-alert-watch"].status == check_result.STATUS_UNKNOWN
    assert results["health-audit"].status == check_result.STATUS_UNKNOWN


# ── Пороги настраиваемы (доказуемость мутацией порогов) ─────────────────────

def test_threshold_min_sample_size_gates_the_verdict():
    """Понижение MIN_SAMPLE_SIZE ниже 1 превращает молодой dependabot-alert
    (N=1) из unknown() в реальный вердикт (здесь — ok(), доля закрытия 0%
    выше порога только если DEAD_CLOSE_RATE_MAX тоже 0 — берём close_rate
    ниже порога специально, чтобы показать переход в violation()."""
    stats = producer_orphan_watch.compute_stats(DEPENDABOT_1, NOW)
    result = producer_orphan_watch.evaluate_producer(
        stats["dependabot-alert-watch"], min_sample=1, min_oldest_open_hours=0)
    assert result.status == check_result.STATUS_VIOLATION
