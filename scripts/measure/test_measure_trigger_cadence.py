#!/usr/bin/env python3
"""Тесты measure_trigger_cadence.py — воспроизводимого замера каденции
триггера (ревизия #1184, находка C: замер, которым калиброваны пороги,
обязан повторяться одной командой, иначе «переизмерить» нечем).

Тесты кормятся ПРОД-ФОРМОЙ (fixtures/quota_watch_completed_runs_7d_2026-09.
json — реальный ответ Actions API, страницы 1-6, снят живым gh api
2026-09-17T11:20Z, обрезан до читаемых полей), не пересказом: живой случай
распределения (разрыв (67.3, 120.6) из замера #1184 сжался до (89.2, 100.1)
за одну неделю) — данные, на которых вердикты ниже и рассчитаны.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent

_spec = importlib.util.spec_from_file_location(
    "measure_trigger_cadence", _HERE / "measure_trigger_cadence.py")
mtc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mtc)

FIXTURE = _HERE / "fixtures" / "quota_watch_completed_runs_7d_2026-09.json"


def _fixture_runs() -> list[dict]:
    return mtc.load_runs_from_file(str(FIXTURE))


def test_fixture_is_prod_form_real_api_shape():
    """Фикстура — прод-форма (реальные id/updated_at/conclusion), не пересказ:
    385 завершённых прогонов quota-watch.yml за 7 суток до 2026-09-17."""
    runs = _fixture_runs()
    assert len(runs) == 385
    assert all(r.get("id") and r.get("conclusion") and r.get("updated_at") for r in runs)
    assert runs[0]["updated_at"].endswith("Z")


def test_gaps_dedup_by_id_and_sort_by_completion_time():
    runs = _fixture_runs() + [dict(_fixture_runs()[0])]  # дубль страницы → нулевой промежуток
    gaps = mtc.gap_minutes(runs)
    assert len(gaps) == 384  # 385 уникальных прогонов — 1
    assert all(g > 0 for g in gaps)
    assert gaps == sorted(gaps)


def test_threshold_90_on_live_week_thin_margin_with_named_facts():
    """Живой случай: замер #1184 (2026-09-07..09-14) показал чистый разрыв
    (67.3, 120.6) шириной 53 мин; через неделю разрыв вокруг 90 — (89.23,
    100.12), нижний запас 0.77 мин. Вердикт обязан назвать это thin-margin с
    числами, а не «ок» по протухшей калибровке."""
    verdict, facts = mtc.cadence_verdict(mtc.gap_minutes(_fixture_runs()), 90.0,
                                         min_margin=15.0)
    assert verdict == "thin-margin"
    assert facts["nearest_below"] == 89.23
    assert facts["nearest_above"] == 100.12
    assert facts["below_margin"] == 0.77
    assert facts["above_margin"] == 10.12
    assert facts["fires_in_window"] == 15


def test_old_threshold_45_fires_twice_as_often_on_same_sample():
    """Мутация-доказательство калибровки: прежнее значение 45 на том же
    живом сэмпле даёт 30 срабатываний против 15 у 90 — канал простоя стрелял
    бы на рутинных паузах шторма вдвое чаще (именно за это #1184 сняла 45)."""
    gaps = mtc.gap_minutes(_fixture_runs())
    _, old = mtc.cadence_verdict(gaps, 45.0)
    _, new = mtc.cadence_verdict(gaps, 90.0)
    assert old["fires_in_window"] == 30
    assert new["fires_in_window"] == 15


def test_wide_break_verdict_ok():
    """Здоровая калибровка: рабочая масса ≤ 30 мин, затишье 200 мин, порог 90 —
    обе стороны разрыва шире min_margin → ok."""
    gaps = [10.0, 12.5, 15.0, 18.0, 20.0, 22.0, 25.0, 28.0, 30.0, 200.0]
    verdict, facts = mtc.cadence_verdict(gaps, 90.0, min_margin=15.0)
    assert verdict == "ok"
    assert facts["nearest_below"] == 30.0 and facts["nearest_above"] == 200.0


def test_no_upper_reference_when_threshold_never_reached():
    """В окне нет ни одного промежутка ≥ порога — верхнюю сторону разрыва
    подтвердить нечем (порог возможно сильно завышен); вердикт не «ok»."""
    gaps = [10.0, 15.0, 20.0, 25.0, 60.0]
    verdict, facts = mtc.cadence_verdict(gaps, 90.0)
    assert verdict == "no-upper-reference"
    assert facts["nearest_above"] is None


def test_no_lower_reference_when_threshold_below_working_mass():
    """Блокер ai-review PR #1185 (круг 4): порог ниже ВСЕЙ рабочей массы —
    `nearest_below` пуст, прежний предикат пропускал None-маржу и отдавал
    «ok» (исполнено ревью: `cadence_verdict([60, 70, 90], 30.0, 15.0)` →
    `("ok", fires_in_window=3)` — спокойный зелёный на пороге, стреляющем
    на каждом тике). Обязан быть no-lower-reference с Named фактами."""
    verdict, facts = mtc.cadence_verdict([60.0, 70.0, 90.0], 30.0, min_margin=15.0)
    assert verdict == "no-lower-reference"
    assert facts["nearest_below"] is None
    assert facts["fires_in_window"] == 3


def test_no_lower_reference_exit_one_through_cli(tmp_path):
    """Тот же вход через CLI (--check + --from-file): код 1, вердикт в
    машинном JSON — интеграция предиката с кодом возврата. Фикстура-минимум
    той же прод-формы (у недельной фикстуры выше промежутки от 0.2 мин —
    порог 30 там даёт thin-margin, не no-lower-reference)."""
    runs = {"workflow_runs": [
        {"id": 1, "conclusion": "success", "updated_at": "2026-09-17T10:00:00Z"},
        {"id": 2, "conclusion": "success", "updated_at": "2026-09-17T11:00:00Z"},
        {"id": 3, "conclusion": "success", "updated_at": "2026-09-17T12:10:00Z"},
        {"id": 4, "conclusion": "success", "updated_at": "2026-09-17T13:40:00Z"},
    ]}
    fixture = tmp_path / "runs.json"
    fixture.write_text(json.dumps(runs), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(_HERE / "measure_trigger_cadence.py"),
         "quota-watch.yml", "--from-file", str(fixture), "--check", "30", "--json"],
        capture_output=True, encoding="utf-8")
    assert result.returncode == 1, result.stderr
    assert json.loads(result.stdout)["verdict"] == "no-lower-reference"


def test_cli_check_exit_codes_and_json_on_fixture():
    """CLI до конца, без сети (--from-file): порог из quota_watch (90.0) —
    thin-margin → код 1 и машинный JSON несёт вердикт и факты; заведомо
    свободный порог 200 с узким min-margin → код 0."""
    common = [sys.executable, str(_HERE / "measure_trigger_cadence.py"),
              "quota-watch.yml", "--from-file", str(FIXTURE), "--json"]

    def run(*extra):
        return subprocess.run(common + list(extra), capture_output=True,
                              encoding="utf-8")

    thin = run("--check")
    assert thin.returncode == 1, thin.stderr
    payload = json.loads(thin.stdout)
    assert payload["verdict"] == "thin-margin"
    assert payload["threshold"] == 90.0  # из quota_watch.MEASUREMENT_STALE_MINUTES

    ok = run("--check", "200", "--min-margin", "5")
    assert ok.returncode == 0, ok.stderr
    assert json.loads(ok.stdout)["verdict"] == "ok"
