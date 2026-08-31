#!/usr/bin/env python3
"""Тесты кампании замера хвоста dispatch (scripts/measure/dispatch_tail.py).

Кормятся прод-формой данных: таймстампы GitHub API как в реальных ответах
('2026-08-31T13:05:00Z', с .000 и с +00:00), строки CSV — продукт самих
строителей строк. Арифметика ожиданий в тесте независимая (литералы, не вызов
тех же функций) — зелёный тест доказывает числа, а не пересказывает код.

Запуск: python -m pytest scripts/measure/test_dispatch_tail.py -q
"""

import importlib.util
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("dispatch_tail.py")
spec = importlib.util.spec_from_file_location("dispatch_tail", SCRIPT)
dt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dt)  # type: ignore[union-attr]


def utc(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


# ── Разбор прод-формы таймстампов GitHub ──────────────────────────────────────────


def test_parse_github_time_accepts_api_forms():
    plain = dt.parse_github_time("2026-08-31T13:05:00Z")
    millis = dt.parse_github_time("2026-08-31T13:05:00.000Z")
    offset = dt.parse_github_time("2026-08-31T13:05:00+00:00")
    assert plain == millis == offset == utc(2026, 8, 31, 13, 5)


def test_parse_github_time_rejects_empty():
    with pytest.raises(ValueError):
        dt.parse_github_time("")


# ── Строка замера по данным собственного run'а ────────────────────────────────────

RUN = {
    # прод-форма ответа GET /repos/{repo}/actions/runs/{id} (поля как есть)
    "id": 1234567890,
    "html_url": "https://github.com/mytab0r/edge-harness/actions/runs/1234567890",
    "created_at": "2026-08-31T13:04:52Z",
    "run_started_at": "2026-08-31T13:05:01.123Z",
}


def test_compute_row_prod_form():
    sent = dt.epoch_ms(utc(2026, 8, 31, 13, 4) + timedelta(seconds=50))
    row = dt.compute_row("probe-1", sent, RUN)
    # отправили 13:04:50.000, run создан 13:04:52.000, стартовал 13:05:01.123
    assert row["latency_ms"] == "11123"
    assert row["queue_ms"] == "9123"
    assert row["status"] == "ok"
    assert row["timeout_s"] == ""
    assert row["run_id"] == "1234567890"
    assert row["run_url"].endswith("/runs/1234567890")


def test_timeout_row_shape_matches_baseline_semantics():
    row = dt.timeout_row("probe-2", 1234567890123)
    assert row["status"] == "timeout"
    assert row["timeout_s"] == "600"  # как у базового замера 2026-08-28
    assert row["latency_ms"] == ""    # в числовую сводку не попадает


def test_csv_roundtrip_keeps_all_rows_and_header():
    rows = [
        dt.compute_row("a", dt.epoch_ms(utc(2026, 8, 31, 13, 4) + timedelta(seconds=50)), RUN),
        dt.timeout_row("b", dt.epoch_ms(utc(2026, 8, 31, 14, 0))),
        dt.dispatch_failed_row("c", dt.epoch_ms(utc(2026, 8, 31, 15, 0)), "HTTP 403: лимит"),
    ]
    text = dt.rows_to_csv(rows)
    back = dt.read_rows(text)
    assert back == rows
    assert text.splitlines()[0].split(",") == dt.CSV_FIELDS
    assert dt.read_rows("") == []


# ── Американское бизнес-окно: пн–пт 13:00–24:00 UTC ──────────────────────────────


@pytest.mark.parametrize("moment,expected", [
    (utc(2026, 8, 31, 12, 59), False),  # пн, до окна
    (utc(2026, 8, 31, 13, 0), True),    # пн, начало окна (9:00 ET)
    (utc(2026, 9, 4, 23, 59), True),    # пт, конец окна (17:00 PT)
    (utc(2026, 9, 5, 13, 0), False),    # сб
    (utc(2026, 9, 6, 15, 0), False),    # вс
    (utc(2026, 9, 7, 10, 0), False),   # пн, но до окна
])
def test_us_business_window(moment, expected):
    assert dt.is_us_business(moment) is expected


# ── Критерий покрытия задачи #4 ──────────────────────────────────────────────────


def make_rows(n: int, start: datetime, step: timedelta, latency_ms: int = 8000) -> list[dict]:
    """n ok-строк с равномерным шагом от start (реальные даты, прод-форма CSV)."""
    return [
        {"probe_id": f"p{i}", "sent_at": str(dt.epoch_ms(start + i * step)),
         "run_id": str(10**6 + i), "run_url": f"https://github.com/r/actions/runs/{10**6 + i}",
         "run_created_at": "2026-08-31T13:04:52Z", "run_started_at": "2026-08-31T13:05:01Z",
         "queue_ms": "6000", "latency_ms": str(latency_ms), "status": "ok",
         "timeout_s": "", "note": ""}
        for i in range(n)
    ]


def test_coverage_met():
    # 100 замеров за 33 часа с шагом 20 мин, старт в пн 12:00 UTC: бизнес-строк ≥ 25
    rows = make_rows(100, utc(2026, 8, 31, 12, 0), timedelta(minutes=20))
    cov = dt.coverage(rows, now=utc(2026, 9, 2, 0))
    assert cov["met"] and cov["ok_rows"] == 100
    assert cov["business_rows"] >= 25
    assert cov["span_h"] >= 24


def test_coverage_requires_business_window():
    rows = make_rows(100, utc(2026, 9, 5, 0, 0), timedelta(minutes=20))  # сб-вс, окно мимо
    cov = dt.coverage(rows, now=utc(2026, 9, 2, 0))
    assert cov["business_rows"] < 25 and not cov["met"]


def test_coverage_requires_100_rows():
    rows = make_rows(99, utc(2026, 8, 31, 12, 0), timedelta(minutes=20))
    assert not dt.coverage(rows, now=utc(2026, 9, 2, 0))["met"]


def test_coverage_requires_24h_span():
    rows = make_rows(100, utc(2026, 8, 31, 13, 0), timedelta(minutes=10))  # ~16.5 ч
    cov = dt.coverage(rows, now=utc(2026, 9, 2, 0))
    assert cov["span_h"] < 24 and not cov["met"]


def test_coverage_overdue_guard():
    rows = make_rows(3, utc(2026, 8, 31, 13, 0), timedelta(minutes=20))
    cov = dt.coverage(rows, now=utc(2026, 9, 10, 0))
    assert cov["overdue"] and not cov["met"]


def test_timeouts_do_not_count_as_measurements():
    rows = make_rows(10, utc(2026, 8, 31, 13, 0), timedelta(minutes=20))
    rows += [dt.timeout_row(f"t{i}", dt.epoch_ms(utc(2026, 9, 1, 0) + timedelta(hours=i)))
             for i in range(90)]
    cov = dt.coverage(rows, now=utc(2026, 9, 2, 0))
    assert cov["ok_rows"] == 10 and not cov["met"]


# ── Сводка: числа считаются независимо, текст содержит их и улики ────────────────


def summary_fixture() -> list[dict]:
    # 10 ok-строк: 8 вне окна (вс 2026-08-30, 12:00–13:40), 2 в окне (пн 13:00+)
    latencies_off = [1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000]
    latencies_on = [9000, 10000]
    rows = []
    for i, ms in enumerate(latencies_off):
        rows.append({**make_rows(1, utc(2026, 8, 30, 12, 0) + timedelta(minutes=i * 10), timedelta(), ms)[0]})
    for i, ms in enumerate(latencies_on):
        rows.append({**make_rows(1, utc(2026, 8, 31, 13, 0) + timedelta(minutes=i * 10), timedelta(), ms)[0]})
    for i, row in enumerate(rows):  # уникальные run_id: улики различимы по ссылке
        row["run_id"] = str(1000000 + i)
        row["run_url"] = f"https://github.com/r/actions/runs/{1000000 + i}"
    rows.append(dt.timeout_row("t1", dt.epoch_ms(utc(2026, 8, 31, 14, 0))))
    rows.append(dt.dispatch_failed_row("f1", dt.epoch_ms(utc(2026, 8, 31, 15, 0)), "HTTP 403"))
    return rows


def test_summarize_numbers_and_evidence():
    text = dt.summarize(summary_fixture())
    # всего ok = 10: медиана (5000+6000)/2 = 5.5 с; p90 → 9-й по порядку = 9000
    assert "**Замеров: 10**" in text
    assert "медиана **5.5 с**" in text
    assert "p90 9.0 с" in text
    assert "max 10.0 с" in text
    # бакеты: бизнес-окно 2 замера медиана 9.5 с; прочее 8 замеров медиана 4.5 с
    assert "медиана 9.5 с, max 10.0 с (2 замеров)" in text
    assert "медиана 4.5 с" in text
    # улики: ссылка на худший run и сверка с базовыми 22 замерами
    assert "runs/1000009" in text
    assert "8.3 с" in text
    # таймаут и сбой диспатча не пропадают молча
    assert "Таймаутов (>600 с без старта): 1" in text
    assert "Сбоев диспатча: 1" in text


def test_summarize_empty_is_loud():
    assert "Успешных замеров нет" in dt.summarize([])


# ── Вставка сводки в доку между маркерами ────────────────────────────────────────


def test_splice_replaces_only_marked_block():
    doc = ("# Дока\n\n## Измерено\n\n" + dt.MARK_BEGIN + "\nстарый текст\n" + dt.MARK_END
           + "\n\n## Хвост файла\n")
    out = dt.splice_summary(doc, "новая сводка")
    assert "старый текст" not in out
    assert "новая сводка" in out
    assert out.startswith("# Дока")
    assert out.endswith("## Хвост файла\n")
    assert out.count(dt.MARK_BEGIN) == 1 and out.count(dt.MARK_END) == 1


def test_splice_without_markers_fails_loud():
    with pytest.raises(ValueError):
        dt.splice_summary("# Дока без маркеров\n", "текст")


# ── CLI: status читает CSV и не падает ───────────────────────────────────────────


def test_status_cli(tmp_path):
    csv = tmp_path / "camp.csv"
    csv.write_text(dt.rows_to_csv(summary_fixture()), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "status", "--csv", str(csv)],
        capture_output=True, text=True, cwd=SCRIPT.parents[2])
    assert proc.returncode == 0, proc.stderr
    assert "Кампания: 10/100 замеров" in proc.stdout
    assert "медиана **5.5 с**" in proc.stdout


# ── Git-транспорт записи: настоящий git, локальный bare-репозиторий ──────────────


def git_writer_fixture(tmp_path, name):
    """Клон data-ветки из локального bare (как clone_data_branch в CI).

    Bare сеется один раз на тест: оба писателя гонки обязаны делить один
    remote — это и есть сценарий append_and_push. --initial-branch обязателен:
    без него bare начинается с master (или чего потребует локальный
    init.defaultBranch), и поведение фикстуры зависит от машины."""
    bare = tmp_path / "bare.git"
    if not bare.exists():
        subprocess.run(["git", "init", "--quiet", "--bare", "--initial-branch=main",
                        str(bare)], check=True)
        seed = tmp_path / "seed"
        subprocess.run(["git", "clone", "--quiet", str(bare), str(seed)], check=True)
        subprocess.run(["git", "-C", str(seed), "checkout", "--quiet", "-B", "main"],
                       check=True)
        (seed / "README.md").write_text("x", encoding="utf-8")
        subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
        subprocess.run(["git", "-C", str(seed), "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "--quiet", "-m", "seed"], check=True)
        subprocess.run(["git", "-C", str(seed), "push", "--quiet", "origin", "main"],
                       check=True)
    work = tmp_path / name
    subprocess.run(["git", "clone", "--quiet", str(bare), str(work)], check=True)
    subprocess.run(["git", "-C", str(work), "checkout", "--quiet", "-B",
                    dt.DATA_BRANCH, "origin/main"], check=True)
    return bare, work


def test_append_and_push_writes_row_and_survives_race(tmp_path):
    bare, writer_a = git_writer_fixture(tmp_path, "a")
    # второй писатель на той же ветке — гонка, которую кампания обязана переживать
    _, writer_b = git_writer_fixture(tmp_path, "b")
    row = dt.compute_row("probe-x", dt.epoch_ms(utc(2026, 8, 31, 13, 4) + timedelta(seconds=50)), RUN)
    assert dt.append_and_push(str(writer_a), row, "probe probe-x: 11123 ms")
    text = (writer_a / dt.CSV_PATH).read_text(encoding="utf-8")
    assert dt.read_rows(text)[0]["probe_id"] == "probe-x"

    # «конкурент» пишет свою строку независимо и пушит первым
    row_b = dt.compute_row("probe-y", dt.epoch_ms(utc(2026, 8, 31, 13, 9)), RUN)
    assert dt.append_and_push(str(writer_b), row_b, "probe probe-y")
    # writer_a продолжает поверх подвинувшейся ветки — его строка не теряется
    row_c = dt.timeout_row("probe-z", dt.epoch_ms(utc(2026, 8, 31, 14, 0)))
    assert dt.append_and_push(str(writer_a), row_c, "probe probe-z: timeout")

    subprocess.run(["git", "clone", "--quiet", "--branch", dt.DATA_BRANCH,
                    str(bare), str(tmp_path / "check")], check=True)
    final_text = (tmp_path / "check" / dt.CSV_PATH).read_text(encoding="utf-8")
    final = dt.read_rows(final_text)
    assert [r["probe_id"] for r in final] == ["probe-x", "probe-y", "probe-z"]
    # Один терминатор строк на весь формат: append-путь не имеет права писать CRLF
    assert "\r" not in final_text


def test_append_and_push_sanitizes_newlines_at_writer_boundary(tmp_path):
    """Перенос строки в поле (note из чужой ошибки API) не должен доезжать до CSV:
    read_rows разбирает построчно — сломанная строка = потерянные замеры."""
    _, work = git_writer_fixture(tmp_path, "s")
    row = dt.dispatch_failed_row("probe-nl", dt.epoch_ms(utc(2026, 8, 31, 15, 0)),
                                 "HTTP 403:\nлимит\r\nисчерпан")
    assert dt.append_and_push(str(work), row, "probe probe-nl: dispatch_failed")
    text = (work / dt.CSV_PATH).read_text(encoding="utf-8")
    assert len(text.strip().splitlines()) == 2  # заголовок + одна строка замера
    back = dt.read_rows(text)
    assert back[0]["probe_id"] == "probe-nl"
    assert "\n" not in back[0]["note"] and "\r" not in back[0]["note"]
    assert "лимит" in back[0]["note"] and "исчерпан" in back[0]["note"]


def test_every_dispatch_tail_step_has_gh_token():
    """Класс-гвардия «шаг пушит в git без GH_TOKEN»: credential helper gh берёт
    токен из env GH_TOKEN (или hosts.yml) — GH_PIPELINE_PAT он не читает.
    Шаг без GH_TOKEN роняет только не-ok пути, и кампания молча деградирует
    до «только ok», теряя ровно хвост, ради которого существует."""
    import yaml  # в repo-ci ставится рядом с pytest
    workflow = (Path(__file__).parents[2]
                / ".github" / "workflows" / "dispatch-latency-probe.yml")
    data = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    steps = [step for job in data["jobs"].values() for step in job.get("steps", [])
             if "dispatch_tail.py" in (step.get("run") or "")]
    assert steps, "шаги кампании исчезли из workflow — гвардия ослепла"
    for step in steps:
        env = step.get("env") or {}
        assert "GH_TOKEN" in env, (f"шаг «{step.get('name')}» зовёт dispatch_tail.py "
                                   "без GH_TOKEN — его записи (timeout/dispatch_failed) "
                                   "никогда не попадут в CSV")


def test_append_and_push_dedupes_probe_id(tmp_path):
    _, work = git_writer_fixture(tmp_path, "w")
    row = dt.timeout_row("probe-dup", dt.epoch_ms(utc(2026, 8, 31, 14, 0)))
    assert dt.append_and_push(str(work), row, "probe probe-dup: timeout")
    # опоздавший job с тем же probe_id не дублирует строку
    again = dt.compute_row("probe-dup", dt.epoch_ms(utc(2026, 8, 31, 13, 4)), RUN)
    assert dt.append_and_push(str(work), again, "probe probe-dup: late") is False
    rows = dt.read_rows((work / dt.CSV_PATH).read_text(encoding="utf-8"))
    assert len(rows) == 1 and rows[0]["status"] == "timeout"
