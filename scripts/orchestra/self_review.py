#!/usr/bin/env python3
"""Периодическая саморевизия конвейера (issue #1025).

## Зачем

Владелец, 2026-09-12: «Почему не автономный воркфлоу? Что ему не хватает?
Почему он сам не делает то, чего ему не хватает?» Существующие детекторы
(`pulse_guard.py::failure_watch`, `stall_detector.py`, `health_regression.py`)
все реактивны к УЖЕ НАЗВАННОМУ классу дефекта: у каждого есть список сигнатур
или отпечатков, и он ищет именно их. Ни один не задаёт открытый вопрос «что
здесь не сходится» без подсказки класса — а именно так находились все живые
дефекты последних суток (человек смотрел на данные, которые API и так отдавал,
и спрашивал, что странно). Этот модуль — не замена реактивным детекторам, а
отдельный, более редкий и более дорогой проход: даёт агенту сырые данные и
открытый вопрос, второй (дешёвый) проход — чек-лист уже оплаченных классов
из `config/self-review-checklist.json` (ДАННЫЕ, растут без правки этого файла).

## Устройство (трёхшаговый трест-контур, тот же приём, что ai-review.yml)

  1. `gather` (доверенный, read-only токен) — собирает сырой снимок периода
     (прогоны отслеживаемых workflow, churn меток по всему репозиторию,
     свежие комментарии watchdog-issue #120, снимки здоровья
     `data/pipeline-health`, сводка пула задач/PR) и строит промпт.
     Транспортный отказ здесь ФАТАЛЕН (см. `GatherTransportError`) — молчаливый
     пустой дайджест неотличим от «в конвейере всё спокойно», а должен быть
     отличим от «мы не смогли посмотреть» (правило AGENTS.md «fail loud»).
  2. `investigate` (недоверенный, БЕЗ GitHub-токена) — не код этого модуля:
     то же самое, что делает `ai-review.yml`, транспорт —
     `scripts/review/ai_dsh.sh` (переиспользован без изменений: скрипт уже
     общий, ничего специфичного для PR-ревью внутри него нет — он просто
     гоняет $AI_WORK/prompt.md через цепочку провайдеров и пишет
     $AI_WORK/answer.txt).
  3. `apply` (доверенный, write-токен) — разбирает ответ по контракту
     находок, ЖЁСТКО отбрасывает находки без проверяемого факта, дедуплицирует
     (маркер-отпечаток в уже заведённых self-review issue + токенная
     похожесть заголовка по всему открытому пулу, `scripts/lib/
     duplicate_guard.py`), считает суточный потолок, заводит задачи с
     нужной меткой по классу находки и печатает, ПОЧЕМУ задач заведено 0,
     если это так — тишина без причины здесь запрещена (см. `main`).

## Три исхода находки (design.md, «Право достроить/записать»)

  - `дефект`/`белое-пятно` -> обычная задача пула (`task` + `self-review`).
  - `знание`     -> задача с меткой `self-review:knowledge` — тело прямо
                    требует записать вывод в AGENTS.md/docs/research/
                    docs/decisions (правило «вывод, оплаченный инцидентом,
                    живёт в репозитории»), не превращать в разовый фикс.
  - `инструмент`  -> задача с меткой `self-review:instrument` — тело прямо
                    требует PR + доказательство на истории, что новая
                    метрика/гвардия поймала бы уже известный инцидент
                    (design.md, «Право достроить себе инструмент»).

## Честный потолок

Это ОДИН дорогой вызов модели раз в SELF_REVIEW_INTERVAL_HOURS часов над
данными за SELF_REVIEW_WINDOW_HOURS часов — не полнота, а выборка. Пропущенный
инцидент, случившийся и закрывшийся полностью внутри одного интервала между
прогонами, не будет замечен: механизм не заменяет reactive-детекторы (#201/
#477/#830), он ищет то, у чего ещё нет ни отпечатка, ни сигнатуры.

Запуск тестов: python -m pytest scripts/orchestra/test_self_review.py -q
"""

from __future__ import annotations

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
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from pulse_guard import (  # type: ignore[import-not-found]
    WATCHDOG_ISSUE,
    all_issue_comments,
    escalate,
    gh,
    parse_time,
    post_issue_comment,
)

_LIB = Path(__file__).resolve().parents[1] / "lib"
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


review_labels = _load_module("review_labels", _LIB / "review_labels.py")
pool_issue = _load_module("pool_issue", _LIB / "pool_issue.py")
duplicate_guard = _load_module("duplicate_guard", _LIB / "duplicate_guard.py")
pipeline_health = _load_module(
    "pipeline_health", _REPO_ROOT / "scripts" / "measure" / "pipeline_health.py")

# ── Конфигурация (константы этого модуля — одно место правды для саморевизии) ──

CHECKLIST_PATH = _REPO_ROOT / "config" / "self-review-checklist.json"

# Отслеживаемые workflow — надмножество WATCHED_WORKFLOWS предохранителя
# (pulse_guard.py, #477): плюс ревью-гейты, чей провал/петля тоже видны
# только сквозь эту саморевизию (класс (б) из design.md — петля меток на
# ОДНОМ issue, не провал прогона, живёт вне списка pulse_guard вовсе).
WATCHED_WORKFLOWS: tuple[str, ...] = (
    "worker.yml", "hands.yml", "orchestra.yml",
    "deploy-worker.yml", "deploy-dsh-edge.yml",
    "conflict-mechanical-rebase.yml",
    "ai-review.yml", "pr-review.yml",
)

# Период сырых данных и периодичность прогона — РАЗНЫЕ числа (design.md):
# окно шире интервала, чтобы соседние прогоны перекрывались (пропуск одного
# прогона по инфраструктурной причине не теряет находку целиком — она снова
# попадёт в окно следующего прогона; дедуп по отпечатку не даст задаче
# задвоиться).
SELF_REVIEW_WINDOW_HOURS = 72
SELF_REVIEW_INTERVAL_HOURS = 6  # cron .github/workflows/self-review.yml

# Суточный потолок автозаведения — свой счётчик, не общий с failure_watch/
# stall_detector (тот же довод, что уже документирован в pulse_guard.py:
# разные каналы дедупа, разные предметные области; общий бюджет означал бы,
# что нашумевший день reactive-детекторов выедает весь бюджет саморевизии).
# Ниже, чем у соседей (5): находки этого прохода менее формальны (открытый
# вопрос, не сигнатура), поэтому WIP-бюджет на них уже — просмотр вручную
# нескольких находок в день реалистичен, штурмовать пул текстом от модели —
# нет.
SELF_REVIEW_DAILY_CAP = 3

TASK_LABEL = "task"
SELF_REVIEW_LABEL = "self-review"
KNOWLEDGE_LABEL = "self-review:knowledge"
INSTRUMENT_LABEL = "self-review:instrument"

FINDING_CLASS_LABELS = {
    "дефект": (),
    "белое-пятно": (),
    "знание": (KNOWLEDGE_LABEL,),
    "инструмент": (INSTRUMENT_LABEL,),
}

CAP_EXHAUSTED_MARKER = "[self-review: потолок исчерпан"

# ── Пагинация прогонов workflow: адаптивный потолок, не жёсткая одна страница ──
#
# Живая проверка 2026-09-12 (доработка по запросу владельца после первой
# приёмки): агрегат `by_conclusion` в первой версии строился по ОДНОЙ
# странице (per_page=100) — для высокочастотных workflow (orchestra.yml,
# ai-review.yml, pr-review.yml) это МЕНЬШЕ, чем число прогонов за
# SELF_REVIEW_WINDOW_HOURS=72 часа: одна страница физически не может
# покрыть всё окно, и регрессия, случившаяся раньше самого свежего дня,
# срезается молча. Замер реальных чисел на этом репозитории (окно 72ч,
# 2026-09-12): worker.yml — 68 прогонов (1 страница), deploy-dsh-edge.yml —
# 10 (1), deploy-worker.yml — 7 (1), conflict-mechanical-rebase.yml — 20 (1),
# hands.yml — 0 (1), НО orchestra.yml — 700 прогонов (нужно 7 страниц),
# ai-review.yml — 341 (4 страницы), pr-review.yml — 275 (3 страницы).
# Итого на реальном окне — 19 запросов на восемь workflow, не 8: адаптивная
# пагинация (останавливается, как только достигнута граница окна ИЛИ
# страница короче per_page) стоит РОВНО столько, сколько нужно для покрытия
# окна, не больше и не меньше. Жёсткий верхний потолок WORKFLOW_RUNS_MAX_PAGES
# — броня на случай аномально высокой частоты (без него один зависший
# workflow мог бы читать историю без конца, тот же класс, что уже поймала
# живая попытка на gather_label_churn, см. её докстринг ниже): 8 страниц
# (800 прогонов) — с запасом выше измеренного максимума (7 у orchestra.yml).
WORKFLOW_RUNS_MAX_PAGES = 8
WORKFLOW_RUNS_PER_PAGE = 100

# Самые свежие N прогонов на workflow, попадающие в промпт ЯВНЫМ списком
# (позиционные timestamp'ы для человека/модели) — по-прежнему меньше полного
# покрытия: агрегат (`by_conclusion`, теперь честно по ВСЕМУ окну) и
# `merge_correlation` ниже несут сигнал регрессии, явный список — только
# витрина последних событий, раздувать промпт полным окном незачем.
WORKFLOW_RECENT_SAMPLE_CAP = 20

# ── Корреляция слияние → до/после (класс «наше изменение сделало хуже») ──────
#
# Открытый вопрос и churn меток ловят аномалию В МОМЕНТЕ (петля на issue,
# красный workflow россыпью). Регрессия от собственного изменения видна
# только В СРАВНЕНИИ до/после конкретного события — эталон (а) (регрессия
# после мержа #878) ИМЕННО этого класса, и агрегата по всему окну для него
# недостаточно (58% провалов размазаны по 5 суткам, само слияние не названо).
#
# Симметричное окно вокруг каждого слияния в периоде — тот же workflow,
# что уже фигурирует в WATCHED_WORKFLOWS (по умолчанию только worker.yml —
# запрос владельца «как минимум worker.yml»: это единственный workflow,
# который слияние может СОДЕРЖАТЕЛЬНО задеть при следующем запуске воркера,
# в отличие от orchestra.yml/ai-review.yml, чья частота определяется числом
# открытых PR, не качеством кода main).
#
# 8 часов — подобрано ЖИВЫМ замером против известного инцидента #878, не
# круглым числом «на глаз» (честно, не маскируется под нейтральный выбор —
# design.md §5 несёт таблицу целиком): прогнано 3/4/6/8/12 часов против
# реальной истории worker.yml на этом репозитории (2026-09-12).
#   - 3ч/4ч/6ч — на #878 НЕДОСТАТОЧНО прогонов с одной из сторон
#     (compute_before_after возвращает None, MIN_RUNS_PER_SIDE не набран) —
#     слияние в это окно попадает, но сигнала о нём нет вовсе.
#   - 12ч — сигнал ЕСТЬ, но РАЗМЫТ: after-окно уже захватывает начавшееся
#     восстановление (13 прогонов «до» при 30.8% успеха, 12 прогонов
#     «после» при 16.7% — обе стороны почти одинаково нездоровы, дельта
#     всего -0.141, НИЖЕ MERGE_CORRELATION_THRESHOLD — сигнал теряется).
#   - 8ч — минимальное окно, набирающее MIN_RUNS_PER_SIDE с обеих сторон
#     (4 прогона «до», 6 «после») И ловящее именно ОСТРУЮ фазу до начала
#     восстановления: доля успеха 0.75 -> 0.0, дельта -0.75 — далеко выше
#     порога, см. design.md §5.3.
# Число не гарантирует того же для ЛЮБОГО будущего инцидента (честная
# граница — design.md, «Корреляция ≠ причинность»): это ПОДОБРАННЫЙ, а не
# теоретически выведенный параметр, годный до тех пор, пока новый живой
# случай не покажет, что нужен другой.
MERGE_CORRELATION_WORKFLOWS: tuple[str, ...] = ("worker.yml",)
BEFORE_AFTER_HOURS = 8

# Меньше этого прогонов с одной из сторон — выборка слишком мала, чтобы
# делать вывод (шум единичных прогонов не должен претендовать на сигнал).
MIN_RUNS_PER_SIDE = 3

# Порог заметности расхождения долей успеха (25 процентных пунктов) —
# комфортно ниже измеренного на #878 перепада (-75 п.п. при 8ч окне, см.
# BEFORE_AFTER_HOURS выше и design.md §5.3), не притянут к самому числу:
# порог остаётся отдельным решением от подобранного окна.
MERGE_CORRELATION_THRESHOLD = 0.25

# Потолок страниц при поиске слитых PR в окне — тот же приём и то же
# обоснование, что WORKFLOW_RUNS_MAX_PAGES выше: адаптивно (ранняя
# остановка по границе окна), с бронёй сверху на случай аномального потока
# слияний.
MERGE_LOOKUP_MAX_PAGES = 5

# Сколько пар (слияние, workflow) с заметным расхождением попадает в
# дайджест — потолок витрины, не данных: расхождений может найтись больше
# (живой замер 2026-09-12: 34 пары прошли порог за одну ночь с высокой
# активностью мержей — то же «13 подозреваемых за ночь», что уже измерено
# в PR #970). 20, не 10: #878 на реальных данных занял 13-е место по
# модулю дельты среди активной ночи слияний — потолок витрины обязан
# оставлять запас над измеренным рангом, не быть подогнан ровно под него.
MERGE_CORRELATION_TOP_N = 20


# ── Чек-лист (данные) ────────────────────────────────────────────────────────


def load_checklist(path: Path = CHECKLIST_PATH) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload.get("classes", [])


def render_checklist(classes: list[dict]) -> str:
    lines = []
    for entry in classes:
        lines.append(f"- **{entry['title']}** ({entry['id']}). {entry['look_for']} "
                     f"Пример из истории: {entry['example']}")
    return "\n".join(lines)


# ── Сбор сырых данных (доверенный шаг, fail loud на отказ транспорта) ────────


class GatherTransportError(RuntimeError):
    """Ни один отслеживаемый источник не прочитался — это отказ прав/сети
    (403/нет соединения), а не «в конвейере тихо». Не ловится тихо нигде выше
    по стеку (см. AGENTS.md: «отказ прав/транспорта обязан краснить прогон,
    а не возвращать 0»)."""


def _duration_minutes(run: dict) -> float | None:
    started = run.get("run_started_at")
    updated = run.get("updated_at")
    if not started or not updated:
        return None
    return round((parse_time(updated) - parse_time(started)).total_seconds() / 60, 1)


def fetch_workflow_runs_window(repo: str, workflow: str, since: datetime,
                                 max_pages: int = WORKFLOW_RUNS_MAX_PAGES,
                                 per_page: int = WORKFLOW_RUNS_PER_PAGE) -> list[dict]:
    """Прогоны workflow, покрывающие окно `since..now` АДАПТИВНО (не жёстко
    одна страница, см. докстринг WORKFLOW_RUNS_MAX_PAGES выше): читает
    страницы (новые прогоны первыми, стандартный порядок GitHub Actions API)
    и останавливается, как только (а) самый старый прогон страницы старше
    `since` — граница окна достигнута, дальше читать незачем, либо (б)
    страница короче `per_page` — прогонов больше нет. `max_pages` — броня
    сверху на случай аномальной частоты (не даёт читать историю без конца,
    тот же класс защиты, что уже есть у gather_label_churn)."""
    runs: list[dict] = []
    for page in range(1, max_pages + 1):
        payload = gh(
            f"repos/{repo}/actions/workflows/{workflow}/runs"
            f"?per_page={per_page}&page={page}") or {}
        chunk = payload.get("workflow_runs", [])
        if not chunk:
            break
        runs.extend(chunk)
        oldest_on_page = parse_time(chunk[-1]["created_at"])
        if oldest_on_page < since or len(chunk) < per_page:
            break
    return runs


def _summarize_workflow_runs(runs: list[dict], since: datetime,
                               recent_cap: int = WORKFLOW_RECENT_SAMPLE_CAP) -> dict:
    """Чистая свёртка уже полученных прогонов в дайджест одного workflow —
    отделена от сетевого fetch_workflow_runs_window ради теста без мока gh."""
    in_window = [r for r in runs if parse_time(r["created_at"]) >= since]
    by_conclusion: dict[str, int] = {}
    samples = []
    for run in in_window:
        conclusion = run.get("conclusion") or f"in_progress:{run.get('status')}"
        by_conclusion[conclusion] = by_conclusion.get(conclusion, 0) + 1
        samples.append({
            "id": run["id"],
            "event": run.get("event"),
            "conclusion": run.get("conclusion"),
            "status": run.get("status"),
            "created_at": run.get("created_at"),
            "updated_at": run.get("updated_at"),
            "duration_minutes": _duration_minutes(run),
            "html_url": run.get("html_url"),
        })
    return {
        "total_in_window": len(in_window),
        "by_conclusion": by_conclusion,
        "recent": samples[:recent_cap],
    }


def gather_workflow_digest(repo: str, since: datetime,
                            workflows: tuple[str, ...] = WATCHED_WORKFLOWS,
                            raw_out: dict[str, list[dict]] | None = None) -> dict:
    """Прогоны отслеживаемых workflow за окно, адаптивно постранично
    (`fetch_workflow_runs_window` — см. её докстринг и докстринг
    WORKFLOW_RUNS_MAX_PAGES: одна страница НЕ покрывала окно у
    высокочастотных workflow, находка живой доработки 2026-09-12).

    `raw_out`, если передан, получает {workflow: [прогоны в окне]} — ТЕ ЖЕ
    данные, что уже прочитаны для агрегата, без второго сетевого похода:
    `gather_merge_correlation` ниже переиспользует их для сравнения
    до/после слияния, а не запрашивает заново.

    Каждый workflow, который не прочитался (RuntimeError — 403/сеть/удалён),
    получает запись {"error": "<текст>"} — ВИДНО модели и человеку, не молча
    пропущен. Если ВСЕ отказали — это GatherTransportError выше по стеку."""
    digest: dict[str, dict] = {}
    failures = 0
    for workflow in workflows:
        try:
            runs = fetch_workflow_runs_window(repo, workflow, since)
        except RuntimeError as error:
            digest[workflow] = {"error": str(error)}
            failures += 1
            continue
        in_window = [r for r in runs if parse_time(r["created_at"]) >= since]
        if raw_out is not None:
            raw_out[workflow] = in_window
        digest[workflow] = _summarize_workflow_runs(runs, since)
    if failures == len(workflows):
        raise GatherTransportError(
            f"все {len(workflows)} отслеживаемых workflow не прочитались — "
            "похоже на отказ прав/сети GitHub API, не на затишье конвейера")
    return digest


# ── Корреляция слияние → до/после (реализация) ───────────────────────────────


def fetch_recent_merges(repo: str, since: datetime, now: datetime,
                          max_pages: int = MERGE_LOOKUP_MAX_PAGES,
                          per_page: int = 100) -> list[dict]:
    """Слитые PR в окне — `state=closed&sort=updated&direction=desc`
    (сортировка по `updated_at`, не `merged_at` — эндпоинт списка PR не
    поддерживает сортировку по последнему; используется как разумное
    приближение, граница названа честно в design.md: PR, обновлённый уже
    ПОСЛЕ мержа поздней меткой/комментарием, может уйти глубже по списку,
    чем его momento слияния — при заданном `max_pages` это означает
    возможный, но не гарантированный пропуск немногих старых слияний, не
    системную потерю). Останавливается по тому же правилу, что
    `fetch_workflow_runs_window`: страница короче `per_page` или самый
    старый элемент страницы (`updated_at`) старше `since`."""
    merges: list[dict] = []
    for page in range(1, max_pages + 1):
        chunk = gh(
            f"repos/{repo}/pulls?state=closed&sort=updated&direction=desc"
            f"&per_page={per_page}&page={page}") or []
        if not chunk:
            break
        for pull in chunk:
            merged_at = pull.get("merged_at")
            if not merged_at:
                continue
            merged_time = parse_time(merged_at)
            if since <= merged_time <= now:
                merges.append({
                    "number": pull["number"],
                    "title": pull.get("title", ""),
                    "merged_at": merged_at,
                })
        oldest_updated = parse_time(chunk[-1]["updated_at"])
        if oldest_updated < since or len(chunk) < per_page:
            break
    return merges


def compute_before_after(runs: list[dict], merge_time: datetime,
                           half_window_hours: float = BEFORE_AFTER_HOURS,
                           min_runs: int = MIN_RUNS_PER_SIDE) -> dict | None:
    """Чистая функция (без сети): доля успеха прогонов workflow в двух
    симметричных окнах вокруг `merge_time` — `[merge_time - half_window,
    merge_time)` и `[merge_time, merge_time + half_window)`. `runs` —
    список прогонов (`created_at`/`conclusion`), уже отфильтрованный по
    большему окну саморевизии (не нужно второго запроса).

    None — недостаточно данных с ОДНОЙ ИЛИ ОБЕИХ сторон (`min_runs`): шум
    единичных прогонов не должен претендовать на сигнал (design.md,
    честная граница — «до было и так плохо» не считается голым числом
    «после хуже», если само «до» посчитано на паре прогонов)."""
    half_window = timedelta(hours=half_window_hours)
    before_start, after_end = merge_time - half_window, merge_time + half_window
    before = [r for r in runs if before_start <= parse_time(r["created_at"]) < merge_time]
    after = [r for r in runs if merge_time <= parse_time(r["created_at"]) < after_end]
    if len(before) < min_runs or len(after) < min_runs:
        return None

    def success_rate(bucket: list[dict]) -> float:
        successes = sum(1 for r in bucket if r.get("conclusion") == "success")
        return successes / len(bucket)

    before_rate, after_rate = success_rate(before), success_rate(after)
    return {
        "before_total": len(before),
        "before_success_rate": round(before_rate, 3),
        "after_total": len(after),
        "after_success_rate": round(after_rate, 3),
        "delta": round(after_rate - before_rate, 3),
    }


def gather_merge_correlation(repo: str, since: datetime, now: datetime,
                               raw_runs_by_workflow: dict[str, list[dict]],
                               workflows: tuple[str, ...] = MERGE_CORRELATION_WORKFLOWS,
                               threshold: float = MERGE_CORRELATION_THRESHOLD,
                               top_n: int = MERGE_CORRELATION_TOP_N) -> list[dict]:
    """Сырой сигнал «до/после каждого слияния в окне» — НЕ отдельный класс
    чек-листа и не готовый вывод: открытый вопрос сам решает, аномалия это
    или нет (design.md, «Корреляция ≠ причинность»). Best-effort:
    недоступность списка слияний — запись {"error": ...}, не тихий пропуск.

    Только пары (слияние, workflow) с |delta| >= threshold и обеими
    сторонами не меньше MIN_RUNS_PER_SIDE (см. compute_before_after)
    попадают в дайджест — иначе список рос бы на КАЖДОЕ слияние периода
    (их могут быть десятки), большинство из которых не задевает workflow
    вовсе."""
    try:
        merges = fetch_recent_merges(repo, since, now)
    except RuntimeError as error:
        return [{"error": str(error)}]
    entries = []
    for merge in merges:
        merge_time = parse_time(merge["merged_at"])
        for workflow in workflows:
            runs = raw_runs_by_workflow.get(workflow, [])
            result = compute_before_after(runs, merge_time)
            if result is None or abs(result["delta"]) < threshold:
                continue
            entries.append({
                "pr": merge["number"],
                "title": merge["title"],
                "merged_at": merge["merged_at"],
                "workflow": workflow,
                **result,
            })
    entries.sort(key=lambda entry: abs(entry["delta"]), reverse=True)
    return entries[:top_n]


# Потолок числа issue/PR, чьи таймлайны реально запрашиваются (design.md,
# «Цена прогона»): repo-wide `issues/events` без фильтра по времени отдаёт
# события в порядке создания (старые первые) и НЕ поддерживает `since` —
# полный обход постранично означал бы вычитать ВСЮ историю репозитория
# (тысячи событий) на каждый прогон, что и произошло на живой попытке
# 2026-09-12 (процесс пришлось убить руками — не давал ответа минуты).
# Вместо этого — тот же приём, что `scripts/measure/label_churn_203.py`
# уже применяет для открытых PR (таймлайн по issue), но с ГРАНИЦЕЙ по
# недавно ОБНОВЛЁННЫМ issues/PR (`since` НА ЭТОМ эндпоинте поддерживается,
# фильтрует по `updated_at`): по построению не может стоить дороже
# `1 + LABEL_CHURN_MAX_ISSUES` запросов, вне зависимости от размера истории
# репозитория.
LABEL_CHURN_MAX_ISSUES = 40


def gather_label_churn(repo: str, since: datetime, min_toggles: int = 4,
                        top_n: int = 20,
                        max_issues: int = LABEL_CHURN_MAX_ISSUES) -> list[dict]:
    """Churn меток по НЕДАВНО ОБНОВЛЁННЫМ issues/PR (не по одному заранее
    известному номеру) — общий канал для класса «тот же issue то ставят, то
    снимают одну метку» (design.md, случай (б): `waiting_owner_guard`
    крутился на #782 без подсказки номера issue заранее). Таймлайн смотрит
    ТОЛЬКО у issues/PR, тронутых за окно (`GET /issues?since=...&sort=updated`,
    единственная страница, не list_pages — сеть ограничена по построению),
    что ограничивает число дорогих запросов таймлайна потолком `max_issues`
    (сортировка по свежести — самые вероятные кандидаты в петлю не срезаются
    первыми). `labeled`/`unlabeled` считаются как переключения одной и той же
    пары (issue, метка); >= min_toggles за окно — кандидат в петлю, вернётся
    как один пункт дайджеста, не как отдельная находка (решение — за моделью).

    Best-effort: недоступность (403/сеть) — пустой список с явной пометкой
    ошибки первым элементом, не тихий пропуск.

    WATCHDOG_ISSUE (#120) исключается из кандидатов ДО применения потолка
    `max_issues` — живой замер цены прогона (design.md, «Цена одного
    прогона»): это ЕДИНСТВЕННЫЙ таймлайн этого прогона, потребовавший
    несколько десятков страниц (сотни комментариев-эскалаций за годы жизни
    задачи-статуса), потому что timeline несёт ВСЕ события (включая
    комментарии), не только labeled/unlabeled — 10 дорогих запросов ради
    фильтра, отбрасывающего почти все события как нерелевантные. Свои
    комментарии #120 уже несёт `gather_watchdog_comments` (постранично, но
    ограничено окном публикации, не всей историей задачи) — вторая, ещё
    более дорогая проекция того же issue не добавляет нового сигнала."""
    since_param = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        touched = gh(
            f"repos/{repo}/issues?state=all&sort=updated&direction=desc"
            f"&since={since_param}&per_page=100") or []
    except RuntimeError as error:
        return [{"error": str(error)}]
    touched = [entry for entry in touched if entry.get("number") != WATCHDOG_ISSUE]
    counters: dict[tuple[int, str], dict] = {}
    for entry_issue in touched[:max_issues]:
        number = entry_issue.get("number")
        if number is None:
            continue
        try:
            timeline = review_labels.list_timeline(repo, number, gh)
        except RuntimeError as error:
            counters[(number, "__error__")] = {
                "issue": number, "label": "__error__", "toggles": 0,
                "last_at": since_param, "error": str(error)}
            continue
        for event in timeline:
            if event.get("event") not in ("labeled", "unlabeled"):
                continue
            created = event.get("created_at")
            if not created or parse_time(created) < since:
                continue
            label = (event.get("label") or {}).get("name")
            if label is None:
                continue
            key = (number, label)
            counter = counters.setdefault(
                key, {"issue": number, "label": label, "toggles": 0, "last_at": created})
            counter["toggles"] += 1
            if created > counter["last_at"]:
                counter["last_at"] = created
    candidates = [entry for entry in counters.values()
                  if entry.get("toggles", 0) >= min_toggles or "error" in entry]
    candidates.sort(key=lambda entry: entry.get("toggles", 0), reverse=True)
    return candidates[:top_n]


def gather_watchdog_comments(since: datetime, max_items: int = 30) -> list[dict]:
    """Свежие комментарии watchdog-issue (#120) за окно — тот же канал, что
    уже читают предохранитель/детектор простоя, здесь просто отдаётся модели
    как сырой текст, не как разобранные маркеры."""
    try:
        comments = all_issue_comments(os.environ.get("GITHUB_REPOSITORY", ""), WATCHDOG_ISSUE)
    except RuntimeError as error:
        return [{"error": str(error)}]
    fresh = [c for c in comments if parse_time(c["created_at"]) >= since]
    return [{"created_at": c["created_at"], "body": (c.get("body") or "")[:600]}
            for c in fresh[-max_items:]]


def gather_health_snapshots(repo: str, max_items: int = 10) -> list[dict]:
    """Последние снимки `data/pipeline-health` (scripts/measure/
    pipeline_health.py::fetch_history) — единственное место чтения, не вторая
    копия обхода git-ветки данных. Best-effort: ветки/файла может не быть."""
    try:
        rows = pipeline_health.fetch_history(repo)
    except Exception as error:  # noqa: BLE001 — дополняющий, необязательный источник
        return [{"error": str(error)}]
    return rows[-max_items:]


def gather_pool_summary(repo: str) -> dict:
    """Сводка пула — счётчики по меткам, не полный дамп (промпт не должен
    раздуваться сотнями строк заголовков, которые открытый вопрос не читает
    построчно)."""
    try:
        tasks = review_labels.list_pages(
            f"repos/{repo}/issues?state=open&labels={review_labels.label_query_value(TASK_LABEL)}&per_page=100", gh)
        pulls = review_labels.list_pages(
            f"repos/{repo}/pulls?state=open&per_page=100", gh)
    except RuntimeError as error:
        raise GatherTransportError(f"пул задач/PR не прочитан: {error}") from error
    label_counts: dict[str, int] = {}
    for issue in tasks:
        for label in issue.get("labels") or []:
            name = label["name"]
            label_counts[name] = label_counts.get(name, 0) + 1
    return {
        "open_tasks": len(tasks),
        "open_pulls": len(pulls),
        "task_label_counts": label_counts,
    }


def gather_digest(repo: str, now: datetime,
                    window_hours: int = SELF_REVIEW_WINDOW_HOURS) -> dict:
    since = now - timedelta(hours=window_hours)
    raw_runs_by_workflow: dict[str, list[dict]] = {}
    workflow_digest = gather_workflow_digest(repo, since, raw_out=raw_runs_by_workflow)
    digest = {
        "window": {"since": since.isoformat(), "until": now.isoformat(),
                    "hours": window_hours},
        "workflow_runs": workflow_digest,
        # Сырой сигнал «до/после каждого слияния в окне» (design.md, класс
        # «наше изменение сделало систему хуже» — петля меток/красный
        # workflow видны В МОМЕНТЕ, регрессия видна только В СРАВНЕНИИ).
        # Переиспользует уже прочитанные прогоны раскрытые выше
        # (raw_runs_by_workflow) — без второго сетевого похода.
        "merge_correlation": gather_merge_correlation(repo, since, now, raw_runs_by_workflow),
        "label_churn": gather_label_churn(repo, since),
        "watchdog_comments": gather_watchdog_comments(since),
        "health_snapshots": gather_health_snapshots(repo),
        "pool_summary": gather_pool_summary(repo),
    }
    return digest


# ── Промпт ────────────────────────────────────────────────────────────────────


FINDING_CONTRACT = """
## Контракт ответа

Для каждой находки — ровно такой блок (можно несколько, можно ноль):

### НАХОДКА
ЗАГОЛОВОК: <короткая фраза>
КЛАСС: дефект | белое-пятно | знание | инструмент
ФАКТ: <конкретный проверяемый факт — число/дата/команда/ссылка ИЗ приведённых
      выше сырых данных, не пересказ и не гипотеза>
ПОЧЕМУ_ВАЖНО: <одна фраза>
БЛОКИРУЕТСЯ: <номера issue через пробел или "ничем">
### КОНЕЦ НАХОДКИ

Если ничего не нашёл — ответь ровно "НАХОДОК НЕТ" и не выдумывай блок ради
формы. Находка без ФАКТ (или с ФАКТ короче нескольких слов, без числа/команды/
ссылки) не пройдёт проверку и будет отброшена — не трать блок на догадку.
КЛАСС "знание" — вывод о систематическом незнании (не разовый баг), задача по
нему потребует записи в AGENTS.md/docs, а не фикса кода. КЛАСС "инструмент" —
для проверки гипотезы не хватает измерения; задача по нему потребует новой
метрики/гвардии с доказательством на истории, а не самого измерения сейчас.
"""

OPEN_QUESTION = (
    "Ниже — сырые данные конвейера edge-harness за период. Открытый вопрос, "
    "без подсказки класса заранее: что здесь не сходится? Что изменилось без "
    "объяснения? Чего мы не ожидали? Смотри на серии (не единичный провал), "
    "на churn меток (одно и то же issue туда-сюда), на снимки здоровья "
    "(тренд, не точка), на комментарии watchdog-issue (что уже названо, а "
    "что нет), на merge_correlation (доля успеха workflow ДО и ПОСЛЕ "
    "конкретного слияния в period — заметный перепад может значить, что "
    "именно это слияние сделало систему хуже). ВАЖНО про merge_correlation: "
    "это КОРРЕЛЯЦИЯ по времени, не доказанная причинность — в один и тот же "
    "интервал могло слиться НЕСКОЛЬКО PR (окно до/после общее для всех), "
    "смениться внешний провайдер, или совпасть с обычным дневным циклом "
    "нагрузки; называй слияние ПОДОЗРЕВАЕМЫМ, а не виновным, если не видишь "
    "в сырых данных отдельного прямого подтверждения (например текста ошибки "
    "в конкретном упавшем прогоне, совпадающего с тем, что менял этот PR)."
)


def build_prompt(digest: dict, checklist: list[dict]) -> str:
    return (
        f"{OPEN_QUESTION}\n\n"
        "## Сырые данные периода\n\n"
        f"```json\n{json.dumps(digest, ensure_ascii=False, indent=2)}\n```\n\n"
        "## Чек-лист известных классов (второй, дешёвый проход — применяй "
        "ПОСЛЕ открытого вопроса, не вместо него)\n\n"
        f"{render_checklist(checklist)}\n"
        f"{FINDING_CONTRACT}"
    )


# ── Разбор ответа (контракт находок) ─────────────────────────────────────────


class Finding(NamedTuple):
    title: str
    klass: str
    fact: str
    why: str
    blocked_by: str


_FINDING_BLOCK_RE = re.compile(r"### НАХОДКА\s*\n(.*?)### КОНЕЦ НАХОДКИ", re.S)
_FIELD_RE = re.compile(
    r"^(ЗАГОЛОВОК|КЛАСС|ФАКТ|ПОЧЕМУ_ВАЖНО|БЛОКИРУЕТСЯ):\s*(.*)$", re.M)


def parse_findings(answer: str) -> list[Finding]:
    """Разбор блоков «### НАХОДКА .. ### КОНЕЦ НАХОДКИ». Блок без ВСЕХ пяти
    полей отбрасывается целиком (частичный контракт — не контракт, тот же
    довод, что «вердикт обязан нести тело» в AGENTS.md)."""
    findings = []
    for match in _FINDING_BLOCK_RE.finditer(answer or ""):
        body = match.group(1)
        fields = dict(_FIELD_RE.findall(body))
        if not {"ЗАГОЛОВОК", "КЛАСС", "ФАКТ", "ПОЧЕМУ_ВАЖНО", "БЛОКИРУЕТСЯ"} <= fields.keys():
            continue
        findings.append(Finding(
            title=fields["ЗАГОЛОВОК"].strip(),
            klass=fields["КЛАСС"].strip().lower(),
            fact=fields["ФАКТ"].strip(),
            why=fields["ПОЧЕМУ_ВАЖНО"].strip(),
            blocked_by=fields["БЛОКИРУЕТСЯ"].strip(),
        ))
    return findings


_FACT_COMMAND_HINTS = ("gh ", "http://", "https://", "`")


def has_verifiable_fact(fact: str) -> bool:
    """Жёсткий фильтр (design.md, «Непроверенная догадка выбрасывается»):
    ФАКТ обязан выглядеть как конкретная проверка, не как пересказ. Эвристика
    (не доказательство корректности факта — только формы): достаточная длина
    И (цифра, ИЛИ отсылка к команде/ссылке). Живой антипример — #962: находка
    без единого числа/команды, чистая гипотеза "возможно тут проблема с X"."""
    text = (fact or "").strip()
    if len(text) < 15:
        return False
    if re.search(r"\d", text):
        return True
    lowered = text.lower()
    return any(hint in lowered for hint in _FACT_COMMAND_HINTS)

def classify_labels(klass: str) -> tuple[str, ...]:
    extra = FINDING_CLASS_LABELS.get(klass)
    if extra is None:
        # Класс не входит в контракт — считаем это дефектом/белым пятном по
        # умолчанию (fail loud по смыслу: не выбрасываем находку молча, но и
        # не выдумываем несуществующую метку).
        extra = ()
    return (TASK_LABEL, SELF_REVIEW_LABEL, *extra)


def finding_body(finding: Finding, digest_window: dict) -> str:
    parts = [
        f"### Цель\n\nРазобрать находку саморевизии за период "
        f"{digest_window.get('since')} .. {digest_window.get('until')}.",
        f"### Критерий готовности\n\n{finding.why}",
        "### Площадь\n\narea:process",
        f"### Чем блокируется\n\n{finding.blocked_by or 'ничем'}",
        "### Что блокирует\n\nничем",
        f"### Контекст и ссылки\n\nНайдено периодической саморевизией "
        f"(issue #1025, `scripts/orchestra/self_review.py`), класс "
        f"`{finding.klass}`.\n\nФАКТ: {finding.fact}",
    ]
    if finding.klass == "знание":
        parts.append(
            "### Требование по классу «знание»\n\nЭто не разовый фикс: "
            "запиши вывод в AGENTS.md/docs/research//docs/decisions "
            "(правило «вывод, оплаченный инцидентом, живёт в репозитории») "
            "и сошлись на путь файла в PR.")
    if finding.klass == "инструмент":
        parts.append(
            "### Требование по классу «инструмент»\n\nДля проверки гипотезы "
            "не хватает измерения. Построй его (новый скрипт в "
            "scripts/measure/, новая величина снимка здоровья, новая "
            "гвардия) и ПРИЛОЖИ В PR доказательство на истории: покажи, что "
            "новая метрика поймала бы уже известный инцидент.")
    parts.append(
        f"\n\nОтпечаток: `{fingerprint(finding)}`\n\n"
        "### Правила\n\n"
        "- [x] Я прочитал docs/research/30-rejected-alternatives.md и "
        "задача не из отвергнутых\n"
        "- [x] Критерий готовности проверяем по видимому результату")
    return "\n\n".join(parts)


def fingerprint(finding: Finding) -> str:
    """Отпечаток находки — КЛАСС + значимые токены заголовка (без стоп-слов,
    duplicate_guard.tokenize), не полный текст: разные формулировки одного и
    того же вывода обязаны схлопнуться так же, как это уже требует
    duplicate_guard для ручного пути issue-create."""
    import hashlib
    tokens = sorted(duplicate_guard.tokenize(finding.title))
    raw = f"{finding.klass}|{' '.join(tokens)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


_FINGERPRINT_BODY_RE = re.compile(r"Отпечаток: `([0-9a-f]{12})`")


def existing_self_review_fingerprints(repo: str) -> set[str]:
    """Отпечатки уже заведённых self-review issue — ОТКРЫТЫХ и ЗАКРЫТЫХ
    (design.md: закрытая через «невалидно» не должна возвращаться на
    следующем прогоне — живой прецедент класса #962, задача на выдуманный
    дефект дошла до пула и была закрыта). Читает и state=open, и
    state=closed (одна метка self-review, не два независимых обхода
    состояния)."""
    issues = review_labels.list_pages(
        f"repos/{repo}/issues?state=all&labels={review_labels.label_query_value(SELF_REVIEW_LABEL)}&per_page=100", gh)
    found = set()
    for issue in issues:
        match = _FINGERPRINT_BODY_RE.search(issue.get("body") or "")
        if match:
            found.add(match.group(1))
    return found


def open_task_candidates(repo: str) -> list[dict]:
    """Кандидаты для токенной похожести (duplicate_guard) — ВЕСЬ открытый
    пул, не только self-review issues: находка саморевизии может пересекаться
    с обычной задачей, заведённой другим путём (человеком/reactive-детектором)."""
    issues = review_labels.list_pages(
        f"repos/{repo}/issues?state=open&labels={review_labels.label_query_value(TASK_LABEL)}&per_page=100", gh)
    return [{"number": i["number"], "title": i.get("title", ""), "url": i.get("html_url", "")}
            for i in issues if "pull_request" not in i]


class Decision(NamedTuple):
    finding: Finding
    action: str  # "create" | "reject_no_fact" | "duplicate" | "capped"
    detail: str


def decide_findings(findings: list[Finding], known_fingerprints: set[str],
                      candidates: list[dict], remaining_cap: int) -> list[Decision]:
    """Чистая функция решения (без сети) — тестируется без мока gh. Порядок
    находок сохраняется; потолок применяется по мере создания (первые
    remaining_cap валидных уникальных находок получают "create", остальные —
    "capped")."""
    decisions = []
    created = 0
    for finding in findings:
        if not has_verifiable_fact(finding.fact):
            decisions.append(Decision(finding, "reject_no_fact",
                                        "ФАКТ не прошёл проверку (пусто/без цифры и без "
                                        "отсылки к команде/ссылке)"))
            continue
        fp = fingerprint(finding)
        if fp in known_fingerprints:
            decisions.append(Decision(finding, "duplicate",
                                        f"отпечаток {fp} уже среди self-review issues "
                                        "(открытых или закрытых)"))
            continue
        similar = duplicate_guard.find_similar_open_tasks(finding.title, candidates)
        if similar:
            top = similar[0]
            decisions.append(Decision(finding, "duplicate",
                                        f"похоже на открытую #{top['number']} "
                                        f"(score={top['score']:.2f}): {top['title']}"))
            continue
        if created >= remaining_cap:
            decisions.append(Decision(finding, "capped",
                                        "суточный потолок SELF_REVIEW_DAILY_CAP исчерпан"))
            continue
        decisions.append(Decision(finding, "create", "проходит все проверки"))
        created += 1
    return decisions


# ── Суточный потолок (сеть) ───────────────────────────────────────────────────


def self_review_created_today(repo: str, now: datetime) -> int:
    since = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    query = f"repo:{repo} label:{review_labels.label_query_value(SELF_REVIEW_LABEL)} created:>={since.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    payload = gh(f"search/issues?q={query.replace(' ', '+')}") or {}
    return int(payload.get("total_count", 0))


# ── Наблюдаемость (текст, тестируется на содержание) ─────────────────────────


def summarize_decisions(decisions: list[Decision]) -> str:
    if not decisions:
        return "self-review: модель не вернула ни одной находки (ответ пуст или «НАХОДОК НЕТ»)"
    created = [d for d in decisions if d.action == "create"]
    rejected = [d for d in decisions if d.action == "reject_no_fact"]
    dup = [d for d in decisions if d.action == "duplicate"]
    capped = [d for d in decisions if d.action == "capped"]
    lines = [f"self-review: находок разобрано {len(decisions)}, "
             f"заведено {len(created)}, отброшено без факта {len(rejected)}, "
             f"дублей {len(dup)}, отсечено потолком {len(capped)}"]
    for d in decisions:
        lines.append(f"  [{d.action}] {d.finding.title} — {d.detail}")
    return "\n".join(lines)


def cap_exhausted_alert_text(capped_titles: list[str], created_today: int,
                               cap: int) -> str:
    listed = "; ".join(capped_titles[:5])
    more = f" (+{len(capped_titles) - 5} ещё)" if len(capped_titles) > 5 else ""
    return (
        f"⏳ edge-harness: {CAP_EXHAUSTED_MARKER} "
        f"{datetime.now(timezone.utc).date().isoformat()}]\n"
        f"Суточный потолок self-review ({created_today}/{cap}) исчерпан — "
        f"отсечены находки, не потеряны молча: {listed}{more}.\n"
        "Газ: потолок считается по факту создания за последние 24ч и "
        "снимается сам на следующем календарном дне; ручной разбор не нужен."
    )


# ── CLI ────────────────────────────────────────────────────────────────────


def cmd_gather(args: argparse.Namespace) -> int:
    now = datetime.now(timezone.utc)
    try:
        digest = gather_digest(args.repo, now, window_hours=args.hours)
    except GatherTransportError as error:
        print(f"::error::self-review gather: {error}", file=sys.stderr)
        return 1
    checklist = load_checklist()
    prompt = build_prompt(digest, checklist)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "digest.json").write_text(json.dumps(digest, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
    (out_dir / "prompt.md").write_text(prompt, encoding="utf-8")
    print(f"self-review gather: окно {digest['window']['since']}..{digest['window']['until']}, "
          f"промпт {len(prompt)} байт -> {out_dir}")
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    now = datetime.now(timezone.utc)
    answer = Path(args.answer).read_text(encoding="utf-8")
    digest = json.loads(Path(args.digest).read_text(encoding="utf-8")) if args.digest else {
        "window": {}}
    findings = parse_findings(answer)
    known_fingerprints = existing_self_review_fingerprints(args.repo)
    candidates = open_task_candidates(args.repo)
    created_today = self_review_created_today(args.repo, now)
    remaining_cap = max(0, SELF_REVIEW_DAILY_CAP - created_today)
    decisions = decide_findings(findings, known_fingerprints, candidates, remaining_cap)

    for decision in decisions:
        if decision.action != "create":
            continue
        labels = classify_labels(decision.finding.klass)
        title = f"Саморевизия: {decision.finding.title}"
        body = finding_body(decision.finding, digest.get("window", {}))
        created = pool_issue.create_pool_issue(gh, args.repo, title, body, labels)
        print(f"self-review: заведена #{created.get('number')} — {title}")

    summary = summarize_decisions(decisions)
    print(summary)

    capped_titles = [d.finding.title for d in decisions if d.action == "capped"]
    if capped_titles:
        text = cap_exhausted_alert_text(capped_titles, created_today + len(
            [d for d in decisions if d.action == "create"]), SELF_REVIEW_DAILY_CAP)
        result = escalate(args.repo, WATCHDOG_ISSUE, text)
        print(f"self-review: потолок исчерпан — {result}")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as file:
            file.write("## self-review\n\n" + summary + "\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    gather_parser = sub.add_parser("gather")
    gather_parser.add_argument("--repo", required=True)
    gather_parser.add_argument("--hours", type=int, default=SELF_REVIEW_WINDOW_HOURS)
    gather_parser.add_argument("--out", required=True)
    gather_parser.set_defaults(func=cmd_gather)

    apply_parser = sub.add_parser("apply")
    apply_parser.add_argument("--repo", required=True)
    apply_parser.add_argument("--answer", required=True)
    apply_parser.add_argument("--digest", required=False, default=None)
    apply_parser.set_defaults(func=cmd_apply)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
