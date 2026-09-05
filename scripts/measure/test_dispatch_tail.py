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


# ── Цепочка тиков: решение о следующей ступени ───────────────────────────────────


def active_cov():
    return dt.coverage(make_rows(10, utc(2026, 8, 31, 13, 0), timedelta(minutes=20)),
                       now=utc(2026, 9, 1, 0))


def test_should_chain_continues_active_campaign():
    assert dt.should_chain(active_cov(), workflow_on_main=True) == ""


def test_should_chain_stops_on_success_limit_and_inactive_workflow():
    met = dt.coverage(make_rows(100, utc(2026, 8, 31, 12, 0), timedelta(minutes=20)),
                      now=utc(2026, 9, 2, 0))
    assert "покрытие достигнуто" in dt.should_chain(met, workflow_on_main=True)
    overdue = dt.coverage(make_rows(3, utc(2026, 8, 31, 13, 0), timedelta(minutes=20)),
                          now=utc(2026, 9, 10, 0))
    assert "лимит дней" in dt.should_chain(overdue, workflow_on_main=True)
    # неактивная кампания: отказ без новой ступени — иначе красная цепочка зациклится
    assert "неактивна" in dt.should_chain(active_cov(), workflow_on_main=False)


# ── Финализация: успех и недобор обязаны отличаться снаружи ──────────────────────


def test_finalize_outcome_met_marks_pr_ready():
    met = dt.coverage(make_rows(100, utc(2026, 8, 31, 12, 0), timedelta(minutes=20)),
                      now=utc(2026, 9, 2, 0))
    outcome = dt.finalize_outcome(met)
    assert outcome["ready"] is True
    assert outcome["verdict"] == ""
    assert "закрой задачу" in outcome["closing"]


def test_finalize_outcome_unmet_keeps_draft_and_forbids_closing():
    # предохранитель: строк много, но критерий не набран — ровно путь из ревью #108
    unmet = dt.coverage(make_rows(45, utc(2026, 8, 31, 12, 0), timedelta(minutes=20)),
                        now=utc(2026, 9, 10, 0))
    assert unmet["overdue"] and not unmet["met"]
    outcome = dt.finalize_outcome(unmet)
    assert outcome["ready"] is False
    assert "НЕ достигнут" in outcome["verdict"]
    assert "НЕ закрывать" in outcome["closing"]
    assert "закрой задачу" not in outcome["closing"]
    assert "CAMPAIGN_MAX_DAYS" in outcome["closing"]  # путь продления назван явно


# ── Продление кампании — env снаружи кода, а не правка константы ─────────────────


def test_campaign_max_days_env_override(monkeypatch):
    monkeypatch.delenv(dt.CAMPAIGN_MAX_DAYS_ENV, raising=False)
    assert dt.campaign_max_days() == 7
    monkeypatch.setenv(dt.CAMPAIGN_MAX_DAYS_ENV, "30")
    assert dt.campaign_max_days() == 30


def test_coverage_overdue_follows_max_days_argument():
    rows = make_rows(3, utc(2026, 8, 31, 13, 0), timedelta(minutes=20))
    assert dt.coverage(rows, now=utc(2026, 9, 4, 0), max_days=3)["overdue"]
    assert not dt.coverage(rows, now=utc(2026, 9, 4, 0), max_days=10)["overdue"]


# ── Сводка: неравномерность сбора и вырожденная очередь видны ────────────────────


def test_summarize_reports_cadence():
    text = dt.summarize(summary_fixture())
    assert "интервалы между замерами: медиана 10 мин" in text
    # самая длинная щель: 13:10 вс 2026-08-30 → 13:00 пн 2026-08-31
    assert "max 23.8 ч" in text


def test_summarize_marks_zero_queue_as_not_measured():
    rows = summary_fixture()
    for row in rows:
        if row["status"] == "ok":
            row["queue_ms"] = "0"
    text = dt.summarize(rows)
    assert "queue_ms = 0 во всех строках" in text
    assert "не измеряется" in text
    # вырожденная колонка не дублируется статистикой «медиана 0.0 с»
    assert "чистое ожидание раннера (часы GitHub" not in text


def test_summarize_keeps_silent_on_informative_queue():
    assert "queue_ms = 0 во всех строках" not in dt.summarize(summary_fixture())


# ── Git-транспорт финализации: регрессия probe 33937006302 ───────────────────────


def test_rewinding_git_ops_carry_identity_and_never_target_main():
    """Регрессия probe 33937006302: `rebase --autostash origin/main` в
    финализации перепроигрывал ВСЕ коммиты ветки данных (ветка отошла от main
    на сотни коммитов) и падал на пустой коммиттер-идентичности — голый клон
    job'а user.name не знает. Класс «git-операция, переписывающая коммиты»:
    каждая переигрывающая (rebase) идёт под COMMIT_IDENTITY — --abort не
    считается, коммитов не создаёт — и ни одна не таргетит main: ветка данных
    append-only, интеграцию делает мерж PR."""
    rebase_lines = [line.strip() for line in
                    SCRIPT.read_text(encoding="utf-8").splitlines()
                    if '"rebase"' in line]
    assert rebase_lines, "rebase исчез из git-транспорта — гвардия ослепла"
    for line in rebase_lines:
        if "--abort" in line:
            continue
        assert "COMMIT_IDENTITY" in line, (
            f"rebase без коммиттер-идентичности — упадёт на голом клоне: {line[:90]}")
        assert "origin/main" not in line and '"main"' not in line, (
            f"rebase таргетит main — перепишет всю ветку данных: {line[:90]}")


# ── Гвардии workflow: цепочка, след тика, страховочный cron ──────────────────────


def load_workflow():
    import yaml
    path = (Path(__file__).parents[2] / ".github" / "workflows"
            / "dispatch-latency-probe.yml")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_tick_job_chains_and_traces_failed_ticks():
    steps = {s.get("name"): s for s in load_workflow()["jobs"]["tick"]["steps"]}
    chain = steps.get("Цепочка — следующая ступень тика")
    assert chain and chain["if"] == "always()", (
        "цепочка перестала быть always(): один сбой тика роняет каденцию "
        "до страховочного cron")
    assert "dispatch_tail.py chain" in (chain.get("run") or "")
    trace = steps.get("След тика, умершего до диспатча")
    assert trace and trace["if"] == "failure()", (
        "след умершего тика перестал быть failure(): класс «тик умер молча» открыт")
    assert "dispatch_tail.py tick_failed" in (trace.get("run") or "")
    for step in (chain, trace):
        assert "GH_TOKEN" in (step.get("env") or {}), (
            f"шаг «{step.get('name')}» без GH_TOKEN: его запись никогда не попадёт в CSV")


def test_every_dispatch_tail_step_provides_token_the_code_reads():
    """Гвардия-класс «код читает один токен, шаг прокидывает другой»: все команды
    dispatch_tail.py читают GH_PIPELINE_PAT (имя с задачи #6), а credential
    helper'у нужен GH_TOKEN. Шаг, прокидывающий только GH_TOKEN, роняет команду
    с ошибкой про не ту переменную; имя до задачи #6 (GH_DISPATCH_TOKEN)
    возвращаться не должно."""
    steps = [step for job in load_workflow()["jobs"].values()
             for step in job.get("steps", [])
             if "dispatch_tail.py" in (step.get("run") or "")]
    assert steps, "шаги кампании исчезли из workflow — гвардия ослепла"
    code_reads = "GH_PIPELINE_PAT" in (
        SCRIPT.read_text(encoding="utf-8").replace("GH_DISPATCH_TOKEN", ""))
    assert code_reads, "скрипт снова читает GH_DISPATCH_TOKEN — гвардия ждёт GH_PIPELINE_PAT"
    for step in steps:
        env = step.get("env") or {}
        assert "GH_PIPELINE_PAT" in env, (
            f"шаг «{step.get('name')}» зовёт dispatch_tail.py без GH_PIPELINE_PAT — "
            "команда упадёт на пустом токене с ошибкой про не ту переменную")
        assert "GH_DISPATCH_TOKEN" not in env, (
            f"шаг «{step.get('name')}» прокидывает GH_DISPATCH_TOKEN — имя до задачи #6, "
            "код его не читает: секрет под чужим именем")


def test_campaign_max_days_declared_once_at_workflow_level():
    """Гвардия-класс «два job'а читают переменную, а задаётся она в одном месте»:
    CAMPAIGN_MAX_DAYS читают и тик (dispatch), и probe (record→finalize).
    Объявление на уровне шага доходит только до одного job'а — продление кампании
    умирает при рождении: probe досчитает до 7 дней и финализирует снова
    (ревью #108)."""
    data = load_workflow()
    env = data.get("env") or {}
    raw = str(env.get("CAMPAIGN_MAX_DAYS", "")).strip()
    assert raw.isdigit() and int(raw) > 0, (
        "CAMPAIGN_MAX_DAYS не объявлена на верхнем уровне workflow — "
        "путь продления кампании после unmet-финализации мёртв")
    for job_name, job in data["jobs"].items():
        assert "CAMPAIGN_MAX_DAYS" not in (job.get("env") or {}), (
            f"job {job_name} переопределяет CAMPAIGN_MAX_DAYS — второе место правды")
        for step in job.get("steps", []):
            assert "CAMPAIGN_MAX_DAYS" not in (step.get("env") or {}), (
                f"шаг «{step.get('name')}» переопределяет CAMPAIGN_MAX_DAYS — "
                "доходит только до одного job'а")


def test_schedule_cron_is_backup_only_and_off_quarter_hours():
    data = load_workflow()
    on = data.get(True) or data.get("on")  # PyYAML читает ключ `on` как булев True
    crons = [trigger["cron"] for trigger in on["schedule"]]
    assert crons, "страховочный cron исчез — смерть цепочки оставит кампанию без страховки"
    for expr in crons:
        minute = int(expr.split()[0])
        assert minute % 15 != 0, (
            f"cron «{expr}» стоит на четверти часа — задокументированный пик "
            "нагрузки schedule (21-github-actions.md)")
