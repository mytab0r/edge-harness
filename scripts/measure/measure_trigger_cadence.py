#!/usr/bin/env python3
"""Воспроизводимый замер каденции триггера workflow по живому Actions API.

Ревизия #1184 (находка C): окна и пороги, обоснованные «тактом cron»,
протухли, потому что cron `*/15` реально доставляет единицы процентов
(docs/research/21-github-actions.md). Числа пересчитаны от ИЗМЕРЕННОЙ
каденции — но замер жил только текстом комментария у константы: «переизмерить,
если распределение сместится» было нечем. Этот скрипт — носитель
воспроизводимости: замер повторяется одной командой, на живых данных, с
выводом чисел, из которых порог выбирается (ближайшие промежутки вокруг
порога — «разрыв» распределения, его ширина, число срабатываний).

Дрейф — не гипотеза, а наблюдённый факт: калибровка MEASUREMENT_STALE_
MINUTES (#1184, замер 183 прогонов 2026-09-07..09-14) нашла чистый разрыв
(67.3, 120.6) шириной 53 минуты; тот же замер 2026-09-17 (этот скрипт,
385 прогонов 2026-09-11..09-17) показывает разрыв вокруг 90 уже (89.2,
100.1) — ширина 11 минут, нижний запас 0.8 минуты. Распределение сжимается
под растущей PR-активностью, и статический тест с вбитыми границами это
не увидит — см. verdict ниже.

Использование:
    python scripts/measure/measure_trigger_cadence.py quota-watch.yml
        — отчёт по последним ~7 суткам завершённых прогонов;
    python scripts/measure/measure_trigger_cadence.py quota-watch.yml --check
        — то же + код возврата: 0, если обе стороны разрыва вокруг порога
        неуже `--min-margin` (по умолчанию 15 мин), 1 — если порог надо
        пересчитывать. Значение порога без аргумента берётся из
        scripts/measure/quota_watch.py::MEASUREMENT_STALE_MINUTES (одно
        место правды); `--check 90.0` подаёт число явно (чужой константе);
    --from-file FIXTURE — читать локальный JSON той же формы, что отдаёт
        API (прод-форма зафиксирована в fixtures/, тесты идут через него,
        без сети);
    --json — машинный вывод отчёта.

Границы честности (не pretend-точность):
- промежуток между прогонами — не «такт одного триггера»: quota-watch.yml
  тикает от pull_request вперемешку со страховочным schedule, и длинный
  промежуток значит «событий не было», а не «канал сломан»; порог простоя
  ОБЯЗАН лежать выше рабочей массы промежутков, чтобы стрелять только на
  настоящих затишьях (#1184). Скрипт измеряет распределение, решение о
  числе — за калибровщиком;
- сэмпл — окно `--days` одного репозитория, не доказательство на будущее:
  живой случай выше (разрыв 53 мин → 11 мин за одну неделю) именно поэтому
  назван в теле отчёта, а не спрятан.
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent.parent / "lib" / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import argparse
import json
import os
import statistics
import subprocess
import sys
from datetime import datetime, timedelta, timezone

# Завершённые прогоны любых выводов — тик канала: замер каденции считает
# «канал тикнул», а не «канал succeeded» (провал тоже тик: троттлинг/гейт
# сработали). Отбор завершённых — здесь, один раз.
PER_PAGE_DEFAULT = 100
MAX_PAGES_DEFAULT = 20
DAYS_DEFAULT = 7.0
MIN_MARGIN_DEFAULT = 15.0


def load_runs_from_api(repo: str, workflow: str, days: float,
                       per_page: int = PER_PAGE_DEFAULT,
                       max_pages: int = MAX_PAGES_DEFAULT) -> list[dict]:
    """Завершённые прогоны workflow за последние `days` суток. Обход страниц
    ЯВНЫЙ (`page=` в URL, потолок max_pages) — одностраничное чтение здесь
    молча теряло бы хвост за страницей (класс #308,
    scripts/lib/test_pagination_guard.py). Остановка: страница неполная,
    потолок страниц или самый старый прогон страницы ушёл за cutoff."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    runs: list[dict] = []
    for page in range(1, max_pages + 1):
        url = (f"repos/{repo}/actions/workflows/{workflow}"
               f"/runs?status=completed&per_page={per_page}&page={page}")
        result = subprocess.run(["gh", "api", url], capture_output=True,
                                encoding="utf-8")
        if result.returncode != 0:
            raise RuntimeError(f"gh api {url} упал: {(result.stderr or '').strip()}")
        batch = (json.loads(result.stdout) or {}).get("workflow_runs") or []
        runs.extend(batch)
        if len(batch) < per_page:
            break
        oldest = min((r.get("updated_at") or r.get("created_at", "")).replace("Z", "+00:00")
                     for r in batch)
        if datetime.fromisoformat(oldest) < cutoff:
            break
    return [r for r in runs
            if r.get("conclusion") is not None
            and _parse_time(r.get("updated_at") or r.get("created_at")) >= cutoff]


def load_runs_from_file(path: str) -> list[dict]:
    """Прод-форма из файла (fixtures/) — та же структура, что у API, без сети."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data.get("workflow_runs") or []


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def gap_minutes(runs: list[dict]) -> list[float]:
    """Отсортированные промежутки (мин) между завершёнными прогонами. Дедуп
    по id (повторный прогон в двух страницах не должен рождать нулевой
    промежуток), сортировка по updated_at — моменту ЗАВЕРШЕНИЯ (у
    завершённого прогона это момент тика, не постановки в очередь)."""
    seen = {r["id"]: r for r in runs if r.get("id") is not None}
    ordered = sorted(seen.values(),
                     key=lambda r: _parse_time(r.get("updated_at") or r["created_at"]))
    times = [_parse_time(r.get("updated_at") or r["created_at"]) for r in ordered]
    return sorted((later - earlier).total_seconds() / 60.0
                  for earlier, later in zip(times, times[1:]))


def cadence_verdict(gaps: list[float], threshold: float,
                    min_margin: float = MIN_MARGIN_DEFAULT) -> tuple[str, dict]:
    """(вердикт, факты). Верdict о РАЗРЫВЕ вокруг `threshold` — механическая
    часть калибровки («порог обязан лежать между кластерами», #1184):
    - "ok": обе стороны разрыва неуже min_margin — число держится;
    - "thin-margin": разрыв есть, но одна из сторон уже min_margin — порог
      поджимается распределением, пересчитать при следующем замере;
    - "no-upper-reference": в окне нет НИ ОДНОГО промежутка ≥ порога —
      верхнюю сторону разрыва подтвердить нечем (порог возможно сильно
      завышен: канал простаивал бы молча весь сэмпл).
    Решение «какое число ставить» — не здесь: здесь только механика разрыва
    (AGENTS.md «Алерт не гадает»: факт — промежутки; выбор порога —
    калибровка)."""
    below = [g for g in gaps if g < threshold]
    above = [g for g in gaps if g >= threshold]
    nearest_below = below[-1] if below else None
    nearest_above = above[0] if above else None
    facts = {
        "gaps": len(gaps),
        "median": round(statistics.median(gaps), 2) if gaps else None,
        "max": round(gaps[-1], 2) if gaps else None,
        "nearest_below": round(nearest_below, 2) if nearest_below is not None else None,
        "nearest_above": round(nearest_above, 2) if nearest_above is not None else None,
        "below_margin": round(threshold - nearest_below, 2) if nearest_below is not None else None,
        "above_margin": round(nearest_above - threshold, 2) if nearest_above is not None else None,
        "fires_in_window": len(above),
        "min_margin": min_margin,
    }
    if nearest_above is None:
        return "no-upper-reference", facts
    if (facts["below_margin"] is not None and facts["below_margin"] < min_margin) \
            or facts["above_margin"] < min_margin:
        return "thin-margin", facts
    return "ok", facts


def _default_threshold() -> float:
    """Порог из одного места правды — константы quota_watch.py — без ручной
    подстановки числа в команду (вторая копия числа протухла бы молча)."""
    spec = importlib.util.spec_from_file_location(
        "quota_watch", Path(__file__).resolve().parent / "quota_watch.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return float(module.MEASUREMENT_STALE_MINUTES)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("workflow", help="файл workflow, например quota-watch.yml")
    parser.add_argument("--days", type=float, default=DAYS_DEFAULT)
    parser.add_argument("--per-page", type=int, default=PER_PAGE_DEFAULT)
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES_DEFAULT)
    parser.add_argument("--min-margin", type=float, default=MIN_MARGIN_DEFAULT,
                        help="минимальная ширина разрыва с каждой стороны порога (мин)")
    parser.add_argument("--check", nargs="?", const="default", default=None,
                        metavar="THRESHOLD",
                        help="вернуть 1, если порог поджат распределением; "
                             "без значения берётся quota_watch.MEASUREMENT_STALE_MINUTES")
    parser.add_argument("--from-file", help="читать прогоны из локального JSON вместо API")
    parser.add_argument("--json", action="store_true", help="машинный вывод")
    args = parser.parse_args(argv)

    try:
        repo = os.environ["GITHUB_REPOSITORY"]
    except KeyError:
        raise SystemExit("GITHUB_REPOSITORY не задан — укажи репозиторий явно "
                         "(или читай прод-форму из файла: --from-file)")
    try:
        runs = (load_runs_from_file(args.from_file) if args.from_file
                else load_runs_from_api(repo, args.workflow, args.days,
                                        args.per_page, args.max_pages))
    except RuntimeError as error:
        print(f"::error::measure_trigger_cadence: {error}", file=sys.stderr)
        return 2

    gaps = gap_minutes(runs)
    window_from = min((_parse_time(r.get("updated_at") or r["created_at"]) for r in runs),
                      default=None)
    report = {
        "workflow": args.workflow,
        "window_days": args.days,
        "runs": len(runs),
        "window_from": window_from.isoformat() if window_from else None,
    }

    if args.check is None:
        # Без --check — просто факты распределения (медиана/хвост), без
        # вердикта: вердикт осмыслен только относительно порога.
        report.update({
            "gaps": len(gaps),
            "median": round(statistics.median(gaps), 2) if gaps else None,
            "max": round(gaps[-1], 2) if gaps else None,
        })
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=1))
        else:
            print(f"{args.workflow}: {len(runs)} завершённых прогонов за ~{args.days} сут "
                  f"(с {report['window_from']}): промежутков {report['gaps']}, "
                  f"медиана {report['median']} мин, максимум {report['max']} мин")
        return 0

    threshold = _default_threshold() if args.check == "default" else float(args.check)
    verdict, facts = cadence_verdict(gaps, threshold, args.min_margin)
    report.update({"threshold": threshold, "verdict": verdict, **facts})
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        print(f"{args.workflow}: {len(runs)} прогонов, промежутков {facts['gaps']} "
              f"(медиана {facts['median']}, макс {facts['max']} мин); порог {threshold}: "
              f"ближайший ниже {facts['nearest_below']} (запас {facts['below_margin']}), "
              f"ближайший ≥ порога {facts['nearest_above']} (запас {facts['above_margin']}), "
              f"срабатываний в окне {facts['fires_in_window']}")
        print(f"вердикт: {verdict} (min_margin={args.min_margin})")
        if verdict == "thin-margin":
            print("порог поджат распределением — пересчитать от свежего замера "
                  "(как это случилось с разрывом (67.3, 120.6) из #1184 за одну неделю)")
        elif verdict == "no-upper-reference":
            print("в окне нет ни одного промежутка ≥ порога — верхнюю сторону "
                  "разрыва подтвердить нечем, порог возможно завышен")
    return 0 if verdict == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
