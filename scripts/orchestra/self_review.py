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
    recent_runs,
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


def gather_workflow_digest(repo: str, since: datetime,
                            workflows: tuple[str, ...] = WATCHED_WORKFLOWS) -> dict:
    """Прогоны отслеживаемых workflow за окно. Один запрос на workflow
    (per_page=100, тот же приём, что pulse_guard.recent_runs) — окно
    SELF_REVIEW_WINDOW_HOURS кратно интервалу диспатча каждого workflow,
    100 прогонов с запасом покрывает период на всех наблюдаемых.

    Каждый workflow, который не прочитался (RuntimeError — 403/сеть/удалён),
    получает запись {"error": "<текст>"} — ВИДНО модели и человеку, не молча
    пропущен. Если ВСЕ отказали — это GatherTransportError выше по стеку."""
    digest: dict[str, dict] = {}
    failures = 0
    for workflow in workflows:
        try:
            runs = recent_runs(repo, workflow, per_page=100)
        except RuntimeError as error:
            digest[workflow] = {"error": str(error)}
            failures += 1
            continue
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
        digest[workflow] = {
            "total_in_window": len(in_window),
            "by_conclusion": by_conclusion,
            # Самые свежие 20 — достаточно, чтобы увидеть серию/аномалию
            # длительности, не раздувая промпт полным списком за 72ч.
            "recent": samples[:20],
        }
    if failures == len(workflows):
        raise GatherTransportError(
            f"все {len(workflows)} отслеживаемых workflow не прочитались — "
            "похоже на отказ прав/сети GitHub API, не на затишье конвейера")
    return digest


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
    ошибки первым элементом, не тихий пропуск."""
    since_param = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        touched = gh(
            f"repos/{repo}/issues?state=all&sort=updated&direction=desc"
            f"&since={since_param}&per_page=100") or []
    except RuntimeError as error:
        return [{"error": str(error)}]
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
    digest = {
        "window": {"since": since.isoformat(), "until": now.isoformat(),
                    "hours": window_hours},
        "workflow_runs": gather_workflow_digest(repo, since),
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
    "что нет)."
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
