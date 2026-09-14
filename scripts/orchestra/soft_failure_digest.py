#!/usr/bin/env python3
"""Дайджест мягких отказов (#1121): деградация, оформленная как штатная
работа, живёт в ЗЕЛЁНЫХ прогонах и молчит месяцами.

Эталон класса (#1097): быстрый провайдер Claude падал `NO_ADAPTER` на КАЖДОМ
прогоне worker.yml полтора месяца — прогон зелёный (система штатно
деградировала на медленный GLM), находка — `::warning::`-аннотация, которую
никто не читал. Прочёс 2026-09-13 (#1121) нашёл ещё семь работающих экземпляров
того же класса (пп.1-7 задачи), в том числе: `rate_limit_retry_budget_exceeded` (провайдер жрёт общий
бюджет ожидания), красный шаг гвардии `blocked` под `continue-on-error`
(job зелёный, шаг красный), красная e2e-канарейка морды (недетерминированный
гейт), автооткат прода — no-op, но рапортует успех (шаг «успешен», хотя
откатил не туда), провайдеры исчерпаны все разом.

## Два канала сигнала (оба нужны — не дублируют друг друга)

**Канал A — аннотации Checks API.** `GET /commits/{sha}/check-runs`
(отфильтрован по `check_suite_id` прогона — на одном sha могут висеть
check-runs ЧУЖИХ прогонов, живой факт: 12 check-runs на одном sha,
`check_suite_id` этого прогона — только 2 из них) даёт `output.
annotations_count` БЕСПЛАТНО (без похода за самими аннотациями) — второй
вызов (`GET /check-runs/{id}/annotations`) только для check-run с
count > 0. Живой замер (2026-09-13, живой прогон f34571b3): аннотация
`warning` со ВСЕЙ строкой `dsh: NO_ADAPTER: no adapter registered for
provider "anthropic-pool"` присутствует НА CHECK-RUN, чей job — «task»
(worker.yml), при ОБЩЕЙ успешности прогона — аннотации Checks API НЕ
маскируются `continue-on-error` (в отличие от `steps[].conclusion` через
Jobs API, см. находку #887/#896: `conclusion` шага после завершения прогона
уже «success», а не реальный `outcome`). Прямое подтверждение на этом же
репозитории: прогон 34753169458 (finding #4 issue #1121) — `run.conclusion`
и `job.conclusion` оба `success`, у ВСЕХ шагов `steps[].conclusion` тоже
`success` (Jobs API ничего не показывает), но аннотация check-run'а того же
job'а несёт `annotation_level: "failure"`, `message: "Process completed with
exit code 1."` — ИМЕННО аннотация видит то, что Jobs API прячет.

**Канал B — conclusion шагов, но не любых, а УСЛОВНЫХ.** Автооткат прода
(finding #1 issue #1121) не оставляет НИ ОДНОЙ аннотации: команда реально
завершается кодом 0 (grep находит нужную строку в логе `wrangler rollback`),
это не маскировка `continue-on-error`, а настоящий успех — просто ложный
(откатил не на ту версию). Канал A такое не видит в принципе. Единственный
генерический (без имени шага в коде) сигнал: шаг, который в ОКНЕ виден и
`skipped`, и НЕ `skipped` — то есть выполняется по условию (`if:`) и
случай, когда условие сработало, статистически редок. Живой замер (deploy-
dsh-edge.yml, 15 последних завершённых прогонов, 2026-09-13): шаг
«Автооткат прода при красной канарейке/смоуке» — `skipped` в 12 прогонах,
`success` в 3 (34750000094, 34742800780, 34702503576) — состав данных, а не
имя шага, делает его «условным»: `"skipped" in conclusions_seen` и
`non_skipped` непусто.

## Порог и окно

`DIGEST_REPEAT_THRESHOLD = 3` за `DIGEST_WINDOW_HOURS = 24`. Обоснование
числом — ЖИВОЙ прогон на реальном 24-часовом окне (2026-09-13, репозиторий
mytab0r/edge-harness, оба канала, после фильтра канала B по `if:`
исходника + правила «skip — большинство»): 58 различимых групп всего, 17
пересекают порог 3. Крупнейшие: `rate_limit`-класс worker.yml — 72,
провал быстрого провайдера Claude (родовой текст) — 13, из них
`NO_ADAPTER` (детальный вариант с точным текстом причины) — 4 отдельной
группой, `UNKNOWN_MODEL` — 2 (честно НЕ пересекает порог в этом конкретном
окне — см. «Не подтверждено»), continue-on-error шаг(и) job'а `orchestra`
(аннотация «process completed with exit code 1») — 113, e2e-канарейка
деплоя — 2-3 в зависимости от конкретных суток. Разовые события (одиночный
сетевой сбой, `Ingest вернул ошибку`, единичный `workspace.create`) держатся
на 1, порог не пересекают — шум отсеян. Пойман бы эталон #1097 (`NO_ADAPTER`
кратно в день по факту частоты worker.yml) — да, кратно превышает порог уже
в первые часы суток.

Live-прогон нашёл ДОПОЛНИТЕЛЬНО (не из семи находок #1121, но того же
класса): `gh: API rate limit exceeded for installation` — 12+ раз в
`orchestra.yml`/`ai-review.yml` за сутки прогона (живой экземпляр #1100,
активный на момент замера) и «закрытие эпизода DO-пульса в #120 не
оставлено» — 7 раз (тот же корень: запись срывается из-за исчерпанной
квоты). Дайджест поймал ЖИВУЮ, не архивную деградацию тем же механизмом.

## Носитель дедупа/состояния — ОТКРЫТЫЕ issues с меткой SOFT_FAILURE_LABEL

Тот же приём, что `pulse_guard.failure_watch`/`dependabot_alert_watch`
(НЕ маркеры-комментарии в #120 — тот приём копит маркер НАВСЕГДА и не даёт
классу просигналить снова после того, как задачу закрыли и баг вернулся,
находка ревью PR #896/#887 «дедуп молчит на новую причину»): дедуп по
отпечатку в ТЕЛЕ открытой issue (SOFT_FAILURE_FINGERPRINT_MARKER) — задача
закрыта человеком/агентом после фикса → тот же отпечаток, встреченный
заново, заводит НОВУЮ задачу (класс вернулся — сигнал должен повториться,
не молчать). Суточный потолок (SOFT_FAILURE_DAILY_CAP, свой счётчик — не
общий с ci-failure/dependabot-alert, тот же довод, что докстринг
`pulse_guard.FAILURE_WATCH_DAILY_CAP`, п.1-2) — эскалация исчерпания
#120+Telegram один раз в календарный день (`SOFT_FAILURE_CAP_MARKER`).

Газ (что снимает сигнал): 1) класс перестаёт появляться в 24-часовом окне —
дедуп ничего не мешает, просто новых наблюдений нет, задача не заводится
повторно; 2) заведённая задача закрывается человеком/агентом, когда
причина устранена или признана нормой — решение объявленное (тот же приём,
что health_audit/dependabot-alert: сигнал не закрывает сам себя автоматом,
подтверждения по внешнему источнику состояния для этого класса не
существует, в отличие от dependabot alert state).

## Стоимость по API (замер живого прогона 2026-09-13)

Гейт цикла (`digest_due`) — 1 дешёвый вызов (issue_marker_times,
`max_pages=3`) на каждый тик оркестратора (~96 тиков/сутки); при отказе
гейта (не готово) — дальше сеть не тратится, это доминирующий случай
(гейт открыт только 2 раза в сутки, `DIGEST_INTERVAL_HOURS=12`).

Некритичная находка ревью PR #1136 (шестой круг): «1 дешёвый вызов» выше
устарело с пятого круга — `escalate_stale_scan` (см. её докстринг) читает
СВОЙ маркер (`issue_marker_times`, тоже `max_pages=3`) на КАЖДОМ тике,
НЕЗАВИСИМО от `digest_due` (намеренно — иначе устойчивое исчерпание квоты
держало бы `digest_due()` в «пропускаю» и застой никогда бы не
эскалировался). На тике без полного скана это, значит, 2 дешёвых вызова
(`issue_marker_times` дайджеста + `issue_marker_times` эскалации застоя),
не 1 — те же ~96 тиков/сутки, вдвое больше базовой цены холостого тика,
по-прежнему на порядки дешевле полного скана ниже.

Полный скан — на КАЖДЫЙ релевантный прогон окна (замер: 259 прогонов за
сутки — worker 24, hands 0, ai-review ~109, orchestra ~122 без PR-шума,
deploy ~4) — 1 вызов check-runs (аннотации, фильтр check_suite_id) + 1
вызов jobs (conclusion шагов) = 2 базовых вызова/прогон = ~518/скан, плюс
1 вызов списка прогонов НА СТРАНИЦУ (постранично, ~8 вызовов/скан на пять
workflow суммарно) плюс 1 вызов `annotations` на каждый check-run с
`annotations_count>0` (меньшинство прогонов — по замеру ~150-200/скан).
Итог одного полного скана — **≈700-750 вызовов `gh api`**; при
`DIGEST_INTERVAL_HOURS=12` (2 скана/сутки) — **≈1500 вызовов/сутки**
добавки, эскалация/дедуп (открытые issues `soft-failure`, суточный
счётчик) — единицы вызовов сверху, не пересчитывается на каждый
пропущенный тик.

## Не подтверждено

- Порог 3 — из ОДНОГО живого 24-часового окна (2026-09-13), не из истории
  многих дней: `UNKNOWN_MODEL` (2 повтора в этом окне) его не пересекает —
  если этот подкласс регрессирует снова, третий повтор потребуется, прежде
  чем дайджест заведёт задачу. Пересмотр порога по факту накопленной
  истории — отдельная правка, когда снимков накопится достаточно (тот же
  класс честности, что design.md pipeline-health-self-audit, «истории
  снимков ещё не существовало»).
- Кириллица в `message` аннотаций читается GitHub API корректно (проверено:
  `subprocess.run(..., encoding="utf-8")` + запись в файл + чтение через
  отдельный инструмент дают чистый текст) — «мойбейк», увиденный при печати
  прямо в Bash-консоль этой сессии, оказался артефактом кодовой страницы
  самого терминала, не дефектом данных GitHub API.

Запуск: python -m pytest scripts/orchestra/test_soft_failure_digest.py -q
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent.parent / "lib" / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import hashlib
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import yaml

from pulse_guard import (
    WATCHDOG_ISSUE,
    classify_failure_cause,
    escalate,
    gh,
    issue_marker_times,
    parse_time,
    post_issue_comment,
)

_LIB = Path(__file__).resolve().parents[1] / "lib"
_PI_SPEC = importlib.util.spec_from_file_location("pool_issue", _LIB / "pool_issue.py")
pool_issue = importlib.util.module_from_spec(_PI_SPEC)
_PI_SPEC.loader.exec_module(pool_issue)  # type: ignore[union-attr]

_RL_SPEC = importlib.util.spec_from_file_location("review_labels", _LIB / "review_labels.py")
review_labels = importlib.util.module_from_spec(_RL_SPEC)
_RL_SPEC.loader.exec_module(review_labels)  # type: ignore[union-attr]

# rate_guard.fetch_core/should_skip — то же место правды, что уже читает
# шаг «Квота GitHub API — ранняя проверка» в orchestra.yml (#454): `gh api
# rate_limit` не тратит собственную квоту (докстринг rate_guard.py,
# подтверждено замером), второй копии этого вызова не заводим. Порог здесь
# СВОЙ (см. SOFT_FAILURE_QUOTA_THRESHOLD ниже) — общий DEFAULT_THRESHOLD=300
# калиброван под orchestra (150-250 вызовов/прогон), а полный скан дайджеста
# стоит ~700-750 (замер живого прогона, находка ревью PR #1136, блокер 2):
# порог соседей его не защищает, здесь нужен собственный, больше.
_RG_SPEC = importlib.util.spec_from_file_location("rate_guard", _LIB / "rate_guard.py")
rate_guard = importlib.util.module_from_spec(_RG_SPEC)
_RG_SPEC.loader.exec_module(rate_guard)  # type: ignore[union-attr]


# ── Одно место правды на конфигурацию дайджеста ──────────────────────────────

# Пять workflow, названные issue #1121 явно (не WATCHED_WORKFLOWS
# pulse_guard.py — тот реестр авто-обнаруживается диском на другой,
# занятой ветке #887/#896; здесь список ровно тот, что назвала задача).
DIGEST_WORKFLOWS = ("worker.yml", "hands.yml", "ai-review.yml", "orchestra.yml", "deploy-dsh-edge.yml")

DIGEST_WINDOW_HOURS = 24

# Аннотации этих уровней несут содержательный сигнал; "notice" — как правило
# информационная строка (пример живого замера: "интеграция X не сконфигурирована"),
# не деградация — не включена, иначе дайджест шумит настройками из design.md.
DIGEST_ANNOTATION_LEVELS = ("warning", "failure")

# orchestra.yml несёт ДВА job'а (contract — на каждый PR, orchestra — по
# расписанию/диспатчу); контрактные прогоны PR-событий не относятся к
# деградации самого конвейера (см. докстринг модуля) и составляют
# БОЛЬШИНСТВО прогонов workflow-файла (замер 2026-09-13: 120 из 242 за
# сутки) — без фильтра дайджест тратил бы половину бюджета на шум contract.
ORCHESTRA_NOISE_EVENTS = ("pull_request",)

# Обоснование числом — докстринг модуля, раздел «Порог и окно».
DIGEST_REPEAT_THRESHOLD = 3

# Гейт цикла: полный скан — не каждый тик оркестратора (~каждые 15 мин),
# а раз в это число часов — см. докстринг модуля, «Стоимость по API».
# 12ч (2 скана/сутки) — не 4ч, как в первой редакции: живой прогон 2026-09-13
# показал ~700-750 вызовов gh api ЗА ОДИН полный скан (259 релевантных
# прогонов × 2 базовых вызова + ~200 вызовов аннотаций); при интервале 4ч
# (6 сканов/сутки) это ~4500 вызовов/сутки — заметная добавка именно тогда,
# когда репозиторий И БЕЗ ТОГО ловит `API rate limit exceeded for
# installation` (см. «Порог и окно» — сам этот прогон поймал живой #1100).
# 12ч — ~1500 вызовов/сутки, тот же день поймает деградацию (критерий
# #1121 — «в первые сутки»), не полтора месяца.
DIGEST_INTERVAL_HOURS = 12.0

DIGEST_HEARTBEAT_MARKER = "[soft-failure-digest: heartbeat"

# Свой порог квоты (находка ревью PR #1136, блокер 2): общий
# rate_guard.DEFAULT_THRESHOLD=300 калиброван под orchestra САМ (150-250
# вызовов/прогон) и оставляет ~700 запросов/час другим потребителям того же
# часа — полный скан дайджеста (~700-750 вызовов, см. «Стоимость по API»)
# сжирает этот остаток целиком и рискует упасть 403 посреди себя же. Порог
# здесь — с запасом сверху сметы скана: 750 (смета) + 20% (тот же запас,
# что у DEFAULT_THRESHOLD) ≈ 900.
SOFT_FAILURE_QUOTA_THRESHOLD = 900

SOFT_FAILURE_LABEL = "soft-failure"
SOFT_FAILURE_DAILY_CAP = 5
SOFT_FAILURE_FINGERPRINT_MARKER = "<!-- soft-failure-fingerprint: "
SOFT_FAILURE_CAP_MARKER = "[soft-failure-digest: потолок исчерпан"

# Находка ревью PR #1136 (пятый круг, блокер 2): «скан давно не выполнялся
# успешно» — свой отдельный маркер и своя эскалация раз в календарный день,
# тем же приёмом, что SOFT_FAILURE_CAP_MARKER выше. Не путать с
# DIGEST_HEARTBEAT_MARKER — тот пишется КАЖДЫЙ успешный скан, этот маркер
# читается, чтобы обнаружить, что успешных сканов давно не было.
STALE_SCAN_MARKER = "[soft-failure-digest: скан устарел"
# ~2×DIGEST_INTERVAL_HOURS (находка дословно): один пропущенный скан —
# самозалечивание окном 24ч ещё не под угрозой, два подряд пропущенных —
# уже сигнал, что квота/сеть деградируют устойчиво (живой класс #1100).
STALE_SCAN_THRESHOLD_HOURS = DIGEST_INTERVAL_HOURS * 2


# ── Нормализация и отпечаток (чистые функции, без сети) ──────────────────────

_HEX_RE = re.compile(r"\b[0-9a-f]{6,}\b")
_DIGIT_RE = re.compile(r"\d+")
_WS_RE = re.compile(r"\s+")


def normalize_text(raw: str) -> str:
    """Убирает id/hex/числа (тот же приём, что pulse_guard.failure_fingerprint,
    отдельная копия — там функция возвращает готовый хэш и не отдаёт
    нормализованный ТЕКСТ, а дайджесту текст нужен для отображения в таблице,
    не только для группировки)."""
    text = (raw or "").lower()
    text = _HEX_RE.sub("", text)
    text = _DIGIT_RE.sub("N", text)
    text = _WS_RE.sub(" ", text).strip()
    return text[:200]


def group_fingerprint(*parts: str) -> str:
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def relevant_run(workflow: str, run: dict) -> bool:
    """False — прогон исключается из скана целиком (см. ORCHESTRA_NOISE_EVENTS)."""
    if workflow == "orchestra.yml" and run.get("event") in ORCHESTRA_NOISE_EVENTS:
        return False
    return True


REPO_ROOT = Path(__file__).resolve().parents[2]


def step_display_name(step: dict) -> str:
    """GitHub автоименует `uses:`-шаг без `name:` как «Run <uses>» (замер
    2026-09-13: живая аннотация несёт буквально «Run actions/checkout@v7») —
    та же форма нужна здесь, чтобы сверять имя шага из YAML с именем шага из
    Jobs API."""
    name = step.get("name")
    if name:
        return name
    uses = step.get("uses")
    return f"Run {uses}" if uses else "?"


def load_workflow_layout(workflow_path: Path) -> dict[str, dict]:
    """Раскладка исходника workflow для канала B:

    ``{job_key: {"display": имя в Jobs API, "conditional": {имена шагов с `if:`}}}``

    Ключ — YAML-ключ job'а; ``display`` — имя, под которым job виден в
    Jobs API (`GET /runs/{id}/jobs` → `job.name`): без матрицы это
    `job.name` из исходника, а без него — сам YAML-ключ; с матрицей GitHub
    показывает ``"<display> (<значения матрицы через запятую>)"``. Сегодня
    пять workflow дайджеста не переопределяют `name:` и не несут матрицы —
    ключ совпадает с именем API; переименование не должно замалчиваться
    (чеклист ревью PR #1136), поэтому расхождение ловит `match_job_key` и
    вызывающий код печатает наблюдение.

    ``conditional`` (шестой круг ревью PR #1136: докстринг перенесён из
    удалённого `load_conditional_steps` — единственный прод-путь, дальше
    не дублируется вторым, неиспользуемым парсером тех же YAML) — ТОЛЬКО
    шаги, несущие явный `if:` в исходнике. Канал B обязан ограничиваться
    этим множеством — иначе БЕЗУСЛОВНЫЙ шаг, пропущенный лишь потому, что
    job оборвался РАНЬШЕ (не из-за своего `if:`, а из-за отказа/отмены
    более раннего шага), ложно считается «условным» — живой замер
    2026-09-13 (до этого фильтра): «Run actions/checkout@v7»
    (ai-review.yml) — success 178 раз в окне и БЕЗ единого `skipped`-исхода
    в реальности (шаг вообще не несёт `if:`), но при падении job'а НА
    ДРУГОМ шаге все ПОСЛЕДУЮЩИЕ автоматически получают `skipped` от самого
    раннера — тот же признак, что и у настоящего условного шага, без
    разбора source отличить нельзя.

    Локальное чтение файла из уже склонированного репозитория (тот же
    workdir, где выполняется этот скрипт в CI) — без сетевого вызова,
    стоимость нулевая. Отсутствующий/неразбираемый файл — пустой результат
    (канал B для этого workflow просто не находит групп, не падает: чтение
    workflow-исходника — вспомогательная точность, не критичный путь)."""
    try:
        doc = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    if not isinstance(doc, dict):
        return {}
    result: dict[str, dict] = {}
    for job_key, job in (doc.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        steps = [step for step in (job.get("steps") or []) if isinstance(step, dict)]
        conditional = {
            step_display_name(step) for step in steps if "if" in step
        }
        display = job.get("name") or job_key
        result[job_key] = {"display": display, "conditional": conditional}
    return result


def match_job_key(layout: dict[str, dict], api_name: str) -> str | None:
    """YAML-ключ job'а по имени из Jobs API — exact или матричная форма
    ``"<display> ("`` (см. `load_workflow_layout`). None — имя не сопоставилось
    НИ С ОДНИМ job'ом исходника; вызывающий код обязан напечатать об этом
    громкое наблюдение, а не молча пропустить (чеклист ревью PR #1136:
    сопоставление, молчащее при расхождении, — тот же silent-wrong, против
    которого построен канал B)."""
    for job_key, job in layout.items():
        display = job["display"]
        if api_name == display or api_name.startswith(f"{display} ("):
            return job_key
    return None


# ── Чистая агрегация (принимает уже прочитанные записи — тестируется без сети) ──


def add_annotation_record(groups: dict, workflow: str, job_name: str, level: str,
                           message: str, run_time: datetime, run_url: str) -> None:
    normalized = normalize_text(message)
    if not normalized:
        return
    fp = group_fingerprint("note", workflow, job_name, normalized)
    group = groups.setdefault(fp, {
        "kind": "annotation", "workflow": workflow, "job": job_name, "step": None,
        "level": level, "sample": (message or "").strip()[:300],
        "count": 0, "first_seen": run_time, "last_seen": run_time, "run_urls": [],
    })
    group["count"] += 1
    if run_time < group["first_seen"]:
        group["first_seen"] = run_time
    if run_time > group["last_seen"]:
        group["last_seen"] = run_time
    if run_url and run_url not in group["run_urls"] and len(group["run_urls"]) < 3:
        group["run_urls"].append(run_url)


def add_step_occurrences(groups: dict, step_conclusions: dict) -> None:
    """Канал B — см. докстринг модуля: только УСЛОВНЫЕ шаги (отфильтрованы
    по `if:` в исходнике ДО этой функции, см. collect_window), у которых
    `skipped` — БОЛЬШИНСТВО исходов, а не-skipped — МЕНЬШИНСТВО. Живой замер
    2026-09-13: без этого условия шаги ai-review.yml вида `if: steps.facts.
    outputs.go == 'true'` (штатное дорогое/дешёвое решение, срабатывает в
    БОЛЬШИНСТВЕ прогонов — 178 успехов на выборке) тонули бы в одной таблице
    с автооткатом прода (`if: failure() && ...`, срабатывает РЕДКО — 3
    успеха), хотя семантика разная: первое — обычный путь работы, второе —
    редкое отклонение, которое и есть предмет #1121. Условие «skip —
    большинство» отделяет одно от другого без имени шага в коде."""
    for (workflow, job_name, step_name), occurrences in step_conclusions.items():
        skipped = [o for o in occurrences if o["conclusion"] == "skipped"]
        non_skipped = [o for o in occurrences if o["conclusion"] != "skipped"]
        if not skipped or not non_skipped or len(skipped) <= len(non_skipped):
            continue
        by_conclusion: dict[str, list] = {}
        for occurrence in non_skipped:
            by_conclusion.setdefault(occurrence["conclusion"], []).append(occurrence)
        for conclusion, items in by_conclusion.items():
            fp = group_fingerprint("step", workflow, job_name, step_name, conclusion)
            times = [item["run_time"] for item in items]
            group = groups.setdefault(fp, {
                "kind": "step", "workflow": workflow, "job": job_name, "step": step_name,
                "level": conclusion,
                "sample": f"шаг «{step_name}» — {conclusion} (обычно skipped, условный)",
                "count": 0, "first_seen": min(times), "last_seen": max(times), "run_urls": [],
            })
            group["count"] += len(items)
            group["first_seen"] = min(group["first_seen"], *times)
            group["last_seen"] = max(group["last_seen"], *times)
            for item in items:
                url = item.get("run_url")
                if url and url not in group["run_urls"] and len(group["run_urls"]) < 3:
                    group["run_urls"].append(url)


def groups_over_threshold(groups: dict, threshold: int = DIGEST_REPEAT_THRESHOLD) -> list[dict]:
    return sorted(
        ({"fp": fp, **group} for fp, group in groups.items() if group["count"] >= threshold),
        key=lambda g: -g["count"],
    )


_CLASS_LABELS = {
    "infra": "инфраструктура",
    "stale_base": "устаревшая база",
    "defect": "дефект/наблюдение",
}


def group_class(group: dict) -> str:
    """Эвристика (не факт): переиспользует ЕДИНСТВЕННОЕ место правды
    (`pulse_guard.classify_failure_cause`, #1115 — тот же класс «чек не
    различает провал инфраструктуры и настоящий дефект», только источник
    здесь другой — зелёные прогоны, не красные чеки). Список сигнатур
    закрытый и консервативный НАРОЧНО (докстринг pulse_guard.py) —
    нераспознанное классифицируется как `defect` по умолчанию, не
    прощается молча; новые сигнатуры (например «job was not started
    because it repeatedly failed to be acquired», найденные расследованием
    #1115) — правка ОДНОГО общего списка в pulse_guard.py, не второй копии
    здесь (файл занят параллельными PR на момент этой правки — см. отчёт
    PR #1136)."""
    return _CLASS_LABELS[classify_failure_cause(group.get("sample") or "")]


def render_digest_table(groups: dict) -> str:
    """Печатается ЦЕЛИКОМ (не только группы над порогом) — требование #1121
    п.1: «печатается таблица» — видимость всей картины, эскалация (заведение
    задачи) отдельно ограничена порогом. Столбец «класс» — см. group_class:
    эвристика, не приговор, отличить инфраструктурный шум от вероятного
    дефекта на глаз, не гадая по одному тексту руками (находка координатора,
    #1115)."""
    if not groups:
        return "Группы не найдены (окно пусто или без аннотаций/условных шагов)."
    rows = sorted(groups.values(), key=lambda g: -g["count"])
    lines = [
        "| workflow | job/step | count | класс | first seen (UTC) | last seen (UTC) | образец |",
        "|---|---|---|---|---|---|---|",
    ]
    for group in rows:
        loc = group["job"] if group["kind"] == "annotation" else f"{group['job']} → {group['step']}"
        sample = (group["sample"] or "").replace("\n", " ")[:120]
        lines.append(
            f"| {group['workflow']} | {loc} | {group['count']} | {group_class(group)} | "
            f"{group['first_seen'].isoformat()} | {group['last_seen'].isoformat()} | {sample} |"
        )
    return "\n".join(lines)


# ── Сетевые читатели (каждый — своя ответственность, testable через monkeypatch gh) ──


DIGEST_MAX_RUN_PAGES = 5


def fetch_runs(repo: str, workflow: str, since: datetime, max_pages: int = DIGEST_MAX_RUN_PAGES) -> list[dict]:
    """Прогоны workflow, завершённые не раньше `since` — постранично
    (класс #308: тихая обрезка первой страницей молча теряла бы orchestra.yml
    и ai-review.yml целиком — живой замер 2026-09-13: orchestra.yml несёт
    242 прогона/сутки, ai-review.yml — 109-111, оба выше одной страницы
    per_page=100). `created=>=` фильтрует НА СЕРВЕРЕ — короткая страница
    (< 100) значит «дальше по этому фильтру ничего нет», не «окно
    закончилось» — тот же признак остановки, что review_labels.list_pages.
    `max_pages` страниц не хватило дочитать до короткой — громкий отказ
    (класс #308/#120A), не тихая обрезка: 5 страниц (500 прогонов) — с
    запасом ×2 над самым частым из пяти workflow на замере."""
    since_str = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    all_runs: list[dict] = []
    for page in range(1, max_pages + 1):
        payload = gh(
            f"repos/{repo}/actions/workflows/{workflow}"
            f"/runs?status=completed&per_page=100&created=>={since_str}&page={page}"
        ) or {}
        runs = payload.get("workflow_runs")
        if not isinstance(runs, list):
            raise RuntimeError(f"{workflow}: неожиданный ответ списка прогонов ({type(runs).__name__})")
        all_runs.extend(runs)
        if len(runs) < 100:
            break
    else:
        raise RuntimeError(
            f"{workflow}: {max_pages} страниц по 100 не хватило дочитать окно "
            f"{since_str} до короткой страницы — окно недочитано этим запросом")
    return [run for run in all_runs if relevant_run(workflow, run)]


def fetch_annotated_check_runs(repo: str, run: dict) -> list[dict]:
    """check-runs ЭТОГО прогона (фильтр по `check_suite_id` — на одном sha
    могут висеть check-runs чужих прогонов, см. докстринг модуля) с
    annotations_count > 0 — `output.annotations_count` уже в этом ответе,
    второй вызов не нужен, чтобы просто узнать, есть ли что читать."""
    sha = run["head_sha"]
    suite_id = run.get("check_suite_id")
    payload = gh(f"repos/{repo}/commits/{sha}/check-runs?per_page=100") or {}
    check_runs = payload.get("check_runs")
    if not isinstance(check_runs, list):
        raise RuntimeError(f"check-runs {sha[:8]}: неожиданный ответ ({type(check_runs).__name__})")
    return [
        c for c in check_runs
        if (c.get("check_suite") or {}).get("id") == suite_id
        and (c.get("output") or {}).get("annotations_count", 0) > 0
    ]


def fetch_annotations(repo: str, check_run: dict) -> list[dict]:
    annotations = gh(f"repos/{repo}/check-runs/{check_run['id']}/annotations?per_page=100")
    if annotations is None:
        annotations = []
    if not isinstance(annotations, list):
        raise RuntimeError(f"check-run {check_run['id']}: аннотации — неожиданный ответ")
    return annotations


def fetch_jobs(repo: str, run: dict) -> list[dict]:
    payload = gh(f"repos/{repo}/actions/runs/{run['id']}/jobs?per_page=100") or {}
    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        raise RuntimeError(f"jobs прогона {run['id']}: неожиданный ответ ({type(jobs).__name__})")
    return jobs


def collect_window(
    repo: str, now: datetime,
    workflows: tuple = DIGEST_WORKFLOWS, window_hours: float = DIGEST_WINDOW_HOURS,
) -> tuple[dict, list[str], dict]:
    """Полный скан окна: канал A (аннотации) + канал B (условные шаги).
    Сбой чтения одного прогона/workflow — наблюдение, не остановка всего
    скана (тот же приём, что pulse_guard.failure_watch: continue, не raise).

    Третий элемент — `{"workflows_total": N, "workflows_ok": M, ...}` (находка
    ревью PR #1136, блокер 3): «список прогонов НЕ прочитан ни для одного
    workflow» и «прочитан, групп просто нет» — разные факты, вызывающий
    (`soft_failure_digest`) обязан различать их, а не печатать одинаковое
    «группы не найдены» на оба (AGENTS.md, «алерт не гадает» — здесь тот же
    класс: пустой результат должен нести причину пустоты).

    `partial_reads_failed` (находка ревью PR #1136, пятый круг, блокер 1):
    список прогонов workflow может прочитаться ЦЕЛИКОМ (workflow попадает в
    `workflows_read`), а чтение check-runs/jobs/аннотаций для отдельных
    прогонов ВНУТРИ него — падать (rate limit посреди скана, живой #1100).
    Раньше это тонуло ⚠️-строкой в step summary (непрочитываемо, докстринг
    `mark_heartbeat`), а сам workflow всё равно засчитывался «прочитанным» —
    heartbeat закрывал гейт на `DIGEST_INTERVAL_HOURS`, хотя реально
    прочитана была только часть окна этого workflow. Счётчик — по числу
    неудачных попыток на прогон (check-runs ИЛИ jobs ИЛИ аннотации), не по
    workflow одной галочкой: `soft_failure_digest` передаёт его в
    `mark_heartbeat`, чтобы гейт называл честную степень покрытия, не только
    факт «workflow вообще тронут»."""
    since = now - timedelta(hours=window_hours)
    groups: dict[str, dict] = {}
    observations: list[str] = []
    step_conclusions: dict[tuple, list] = {}
    workflows_ok = 0
    workflows_read: list[str] = []
    partial_reads_failed: dict[str, int] = {}

    for workflow in workflows:
        # Локальное чтение (0 сетевых вызовов) — какие шаги ЭТОГО workflow
        # несут явный `if:` в исходнике; канал B ограничен ими (см.
        # докстринг load_workflow_layout — иначе безусловный шаг,
        # пропущенный лишь из-за раннего обрыва job'а, ложно считается
        # «условным»).
        layout = load_workflow_layout(REPO_ROOT / ".github" / "workflows" / workflow)
        conditional_steps = {k: job["conditional"] for k, job in layout.items()}
        # Чеклист ревью PR #1136: у workflow без единого `if:`-шага канал B
        # выродился бы в вызов fetch_jobs на КАЖДЫЙ прогон с выброшенным
        # результатом (worker.yml/hands.yml: ~24 лишних вызова за скан только
        # по worker) — канал B для него отключается, канал A не трогается.
        channel_b_enabled = any(conditional_steps.values())
        try:
            runs = fetch_runs(repo, workflow, since)
        except RuntimeError as error:
            observations.append(f"⚠️ soft-failure-digest {workflow}: список прогонов не прочитан ({error})")
            continue
        workflows_ok += 1
        workflows_read.append(workflow)
        unmatched_jobs: set[str] = set()
        for run in runs:
            run_time = parse_time(run["updated_at"])
            run_url = run.get("html_url", "")

            try:
                annotated = fetch_annotated_check_runs(repo, run)
            except RuntimeError as error:
                observations.append(f"⚠️ soft-failure-digest {workflow}: check-runs {run_url} не прочитаны ({error})")
                partial_reads_failed[workflow] = partial_reads_failed.get(workflow, 0) + 1
                annotated = []
            for check_run in annotated:
                job_name = check_run.get("name", "?")
                try:
                    annotations = fetch_annotations(repo, check_run)
                except RuntimeError as error:
                    observations.append(
                        f"⚠️ soft-failure-digest {workflow} (job «{job_name}»): аннотации не прочитаны ({error})")
                    partial_reads_failed[workflow] = partial_reads_failed.get(workflow, 0) + 1
                    continue
                for annotation in annotations:
                    level = annotation.get("annotation_level")
                    if level not in DIGEST_ANNOTATION_LEVELS:
                        continue
                    add_annotation_record(
                        groups, workflow, job_name, level,
                        annotation.get("message") or "", run_time, run_url)

            if not channel_b_enabled:
                continue
            try:
                jobs = fetch_jobs(repo, run)
            except RuntimeError as error:
                observations.append(f"⚠️ soft-failure-digest {workflow}: jobs {run_url} не прочитаны ({error})")
                partial_reads_failed[workflow] = partial_reads_failed.get(workflow, 0) + 1
                continue
            for job in jobs:
                job_name = job.get("name", "?")
                job_key = match_job_key(layout, job_name)
                if job_key is None:
                    # Чеклист ревью PR #1136: job из Jobs API, не сопоставленный
                    # ни с одним job'ом исходника (переименовали, добавили
                    # `name:` или матрицу), раньше пропускался молча — канал B
                    # просто «не находил» шаги. Теперь громкое наблюдение
                    # (одно на workflow за скан), не тишина.
                    unmatched_jobs.add(job_name)
                    continue
                allowed_steps = conditional_steps.get(job_key, set())
                for step in job.get("steps") or []:
                    step_name = step.get("name", "?")
                    if step_name not in allowed_steps:
                        continue
                    conclusion = step.get("conclusion")
                    if conclusion is None:
                        continue
                    key = (workflow, job_key, step_name)
                    step_conclusions.setdefault(key, []).append(
                        {"conclusion": conclusion, "run_time": run_time, "run_url": run_url})
        for job_name in sorted(unmatched_jobs):
            observations.append(
                f"⚠️ soft-failure-digest {workflow}: job «{job_name}» из Jobs API не "
                "сопоставлен ни с одним job'ом исходника — канал B для него пропущен "
                "(переименован в YAML? см. match_job_key)")

    add_step_occurrences(groups, step_conclusions)
    stats = {"workflows_total": len(workflows), "workflows_ok": workflows_ok,
             "workflows_read": workflows_read, "partial_reads_failed": partial_reads_failed}
    return groups, observations, stats


# ── Гейт цикла (не каждый тик — см. докстринг, «Стоимость по API») ──────────


def digest_due(repo: str, now: datetime, interval_hours: float = DIGEST_INTERVAL_HOURS) -> bool:
    """Дешёвый гейт: max_pages=3 — последние ~300 комментариев #120, этого
    достаточно, чтобы найти маркер не старше нескольких часов (тот же приём,
    что max_pages у остальных читателей #120, класс #607: стоимость тика не
    должна расти с историей задачи). Маркер не прочитался — лучше лишний раз
    прогнать скан, чем молчать навсегда (fail loud стороной действия, не
    бездействия)."""
    try:
        times = issue_marker_times(repo, WATCHDOG_ISSUE, DIGEST_HEARTBEAT_MARKER, max_pages=3)
    except RuntimeError:
        return True
    if not times:
        return True
    return (now - max(times)).total_seconds() / 60 >= interval_hours * 60


def mark_heartbeat(repo: str, now: datetime, unread: list[str] | None = None) -> None:
    """Чеклист ревью PR #1136: частичный провал (например, вечный 404 на
    переименованный workflow) раньше закрывал гейт heartbeat'ом, а сам факт
    выпадения жил одной ⚠️-строкой в step summary, которую механически никто
    не читает — ровно класс «наблюдатель не смог посмотреть, но молчит
    зелёным». Теперь непрочитанные workflow НАЗВАНЫ в самом heartbeat'е —
    единственном месте, которое гейт цикла читает гарантированно."""
    suffix = ""
    if unread:
        suffix = (
            "\nНепрочитаны в этом скане (сбой чтения; до самозалечивания окном "
            f"{DIGEST_WINDOW_HOURS}ч классы этих workflow вне покрытия): "
            + ", ".join(unread))
    post_issue_comment(
        repo, WATCHDOG_ISSUE,
        f"{DIGEST_HEARTBEAT_MARKER} {now.isoformat()}]\n"
        "soft-failure-digest: очередной полный скан выполнен." + suffix)


def escalate_stale_scan(repo: str, now: datetime) -> list[str]:
    """Находка ревью PR #1136 (пятый круг, блокер 2): все внутренние отказы
    `escalate_groups`/`quota_sufficient`/`digest_due` раньше оставляли след
    только в step summary (непрочитываемо) — при устойчивом исчерпании квоты
    (живой #1100) дайджест мог сидеть мёртвым днями, а критерий 3 задачи
    («заводит задачу сама») молча переставал выполняться при формально
    зелёной системе. Эскалация по ВОЗРАСТУ последнего УСПЕШНОГО heartbeat'а
    (`DIGEST_HEARTBEAT_MARKER`), не по факту конкретного отказа — не важно,
    КАКОЙ шаг внутри скана деградировал, важно, что скана не было
    `STALE_SCAN_THRESHOLD_HOURS` часов подряд. Раз в календарный день —
    тот же приём, что `SOFT_FAILURE_CAP_MARKER` выше (собственный маркер,
    `issue_marker_times` проверяет, не эскалировали ли уже сегодня).

    Читает маркер НЕЗАВИСИМО от `digest_due`/квоты — вызывается ДО обоих
    (см. `soft_failure_digest`), чтобы застой сообщался, даже если сам скан
    в этом тике пропущен (рано или квоты не хватает): иначе устойчивое
    исчерпание квоты держало бы `digest_due()`/`quota_sufficient()` в
    состоянии «пропускаю» бесконечно, и эта функция никогда бы не вызвалась.

    Сбой чтения самого маркера — не эскалируем вслепую (нечем отличить
    «стухло» от «просто не прочиталось»), только наблюдение."""
    try:
        times = issue_marker_times(repo, WATCHDOG_ISSUE, DIGEST_HEARTBEAT_MARKER, max_pages=3)
    except RuntimeError as error:
        return [f"::warning::soft-failure-digest: маркер heartbeat не прочитан ({error}) — "
                "возраст последнего успешного скана неизвестен"]
    if not times:
        # Ни одного heartbeat'а в истории вовсе — либо только что заведён
        # (первый скан ещё не случился), либо очень долгая деградация вне
        # окна max_pages=3. Первое — не деградация, второе неотличимо от
        # первого этим чтением; молчим, не гадаем (AGENTS.md, «алерт не
        # гадает») — ближайший digest_due() всё равно попытается просканировать.
        return []
    age_hours = (now - max(times)).total_seconds() / 3600
    if age_hours < STALE_SCAN_THRESHOLD_HOURS:
        return []
    marker = f"{STALE_SCAN_MARKER} {now.date().isoformat()}]"
    try:
        already_escalated = bool(issue_marker_times(repo, WATCHDOG_ISSUE, marker, max_pages=3))
    except RuntimeError as error:
        return [f"::warning::soft-failure-digest: маркер {STALE_SCAN_MARKER} не прочитан ({error})"]
    if already_escalated:
        return []
    escalate(
        repo, WATCHDOG_ISSUE,
        f"🚨 edge-harness: {marker}\n"
        f"soft-failure-digest не завершал успешный скан {age_hours:.1f}ч "
        f"(порог {STALE_SCAN_THRESHOLD_HOURS:.0f}ч) — конвейер мог деградировать "
        "молча всё это время, критерий #1121 «заводит задачу сама» не "
        "выполняется, пока скан не выполнился.\n\n"
        "Что дальше: посмотреть step summary последних прогонов orchestra "
        "(шаг «Дайджест мягких отказов») и квоту GitHub API "
        "(`gh api rate_limit`) — устойчивое исчерпание квоты (#1100) "
        "самая частая причина.")
    return [f"🚨 soft-failure-digest: скан не выполнялся {age_hours:.1f}ч — эскалировано в #{WATCHDOG_ISSUE}"]


# ── Дедуп/эскалация — по ОТКРЫТЫМ issues SOFT_FAILURE_LABEL, не по #120 ──────
# (см. докстринг модуля, «Носитель дедупа»: закрытая задача не запирает класс
# навсегда — тот же приём, что pulse_guard.failure_watch/ci_failure_fingerprints)


def open_soft_failure_issues(repo: str) -> list[dict]:
    return review_labels.list_pages(
        f"repos/{repo}/issues?state=open"
        f"&labels={review_labels.label_query_value(SOFT_FAILURE_LABEL)}&per_page=100", gh)


def tracked_fingerprints(issues: list[dict]) -> set[str]:
    found = set()
    for issue in issues:
        body = issue.get("body") or ""
        idx = body.find(SOFT_FAILURE_FINGERPRINT_MARKER)
        if idx == -1:
            continue
        rest = body[idx + len(SOFT_FAILURE_FINGERPRINT_MARKER):]
        fp = rest.split(" ", 1)[0].split("-->", 1)[0].strip()
        if fp:
            found.add(fp)
    return found


def tasks_created_since(repo: str, since: datetime) -> int:
    issues = review_labels.list_pages(
        f"repos/{repo}/issues?state=all"
        f"&labels={review_labels.label_query_value(SOFT_FAILURE_LABEL)}&per_page=100", gh)
    return sum(
        1 for issue in issues
        if "pull_request" not in issue and parse_time(issue["created_at"]) >= since
    )


def digest_task_title(group: dict) -> str:
    loc = group["job"] if group["kind"] == "annotation" else f"{group['job']}/{group['step']}"
    return f"Мягкий отказ: {group['workflow']} ({loc}) — {group['count']}x за {DIGEST_WINDOW_HOURS}ч"


def digest_task_body(group: dict, fp: str) -> str:
    urls = "\n".join(f"- {url}" for url in group["run_urls"]) or "(нет сохранённых ссылок)"
    return (
        "## Цель\n"
        f"`{group['workflow']}` перестаёт молча деградировать этим классом "
        "(дайджест мягких отказов, #1121).\n\n"
        "## Критерий готовности\n"
        "Класс не появляется в следующих прогонах дайджеста, либо "
        "задокументирован как ожидаемое поведение (закрыть со ссылкой на "
        "документ/решение).\n\n"
        "## Площадь\n"
        "area:orchestra\n\n"
        "## Факт\n"
        f"Повторов в окне: {group['count']} за {DIGEST_WINDOW_HOURS}ч, "
        f"с {group['first_seen'].isoformat()} по {group['last_seen'].isoformat()}.\n"
        f"Класс (эвристика classify_failure_cause, #1115): {group_class(group)}.\n"
        f"Текст образца: {group['sample']}\n\n"
        f"Живые прогоны:\n{urls}\n\n"
        f"{SOFT_FAILURE_FINGERPRINT_MARKER}{fp} -->\n"
    )


def escalate_groups(repo: str, ranked_groups: list[dict], now: datetime) -> tuple[list[str], list[str]]:
    """Заводит задачу на каждую НОВУЮ группу над порогом (не отслеженную ни
    одной открытой issue SOFT_FAILURE_LABEL), в пределах суточного потолка;
    исчерпание — тихий пропуск + эскалация #120+Telegram один раз в
    календарный день (тот же приём, что pulse_guard.failure_watch/
    dependabot_alert_watch)."""
    observations: list[str] = []
    actions: list[str] = []
    if not ranked_groups:
        return observations, actions

    try:
        open_issues = open_soft_failure_issues(repo)
    except RuntimeError as error:
        # `::warning::` — не `⚠️` (находка ревью PR #1136, пятый круг, блокер
        # 2): текст с эмодзи-префиксом попадает только в step summary
        # (непрочитываемо, докстринг mark_heartbeat) — настоящая аннотация
        # Checks API даёт каналу A этого же дайджеста шанс поймать деградацию
        # самой эскалации на следующем скане, ровно как уже сделано для
        # workflows_ok == 0.
        observations.append(
            f"::warning::soft-failure-digest: список задач {SOFT_FAILURE_LABEL} не прочитан ({error}) — "
            "дедуп недоступен, задачи в этом пульсе не заводятся")
        return observations, actions
    tracked = tracked_fingerprints(open_issues)

    created_today = None
    counter_failed = False
    cap_skipped: list[str] = []
    for group in ranked_groups:
        fp = group["fp"]
        if fp in tracked:
            observations.append(f"🔁 soft-failure-digest: класс {fp} ({group['workflow']}) уже в пуле — не дублирую")
            continue
        if counter_failed:
            observations.append(f"⏭️ soft-failure-digest: класс {fp} не заведён — счётчик потолка не прочитан")
            continue
        if created_today is None:
            try:
                created_today = tasks_created_since(repo, now - timedelta(hours=24))
            except RuntimeError as error:
                counter_failed = True
                observations.append(
                    f"::warning::soft-failure-digest: счётчик суточного потолка не прочитан ({error}) — "
                    f"класс {fp} и следующие в этом пульсе не заводятся")
                continue
        if created_today >= SOFT_FAILURE_DAILY_CAP:
            cap_skipped.append(fp)
            observations.append(
                f"⏭️ soft-failure-digest: класс {fp} отсечён потолком "
                f"({created_today}/{SOFT_FAILURE_DAILY_CAP})")
            continue
        title = digest_task_title(group)
        body = digest_task_body(group, fp)
        try:
            created = pool_issue.create_pool_issue(gh, repo, title, body,
                                                ["task", SOFT_FAILURE_LABEL, "area:orchestra"])
        except RuntimeError as error:
            observations.append(f"::warning::soft-failure-digest: класс {fp} не заведён ({error})")
            continue
        created_today += 1
        actions.append(f"📋 soft-failure-digest: класс {fp} → задача #{created['number']}")

    if cap_skipped:
        marker = f"{SOFT_FAILURE_CAP_MARKER} {now.date().isoformat()}]"
        try:
            already_capped = bool(issue_marker_times(repo, WATCHDOG_ISSUE, marker, max_pages=3))
        except RuntimeError as error:
            observations.append(f"::warning::soft-failure-digest: маркеры #{WATCHDOG_ISSUE} не прочитаны ({error})")
            already_capped = True
        if not already_capped:
            escalate(
                repo, WATCHDOG_ISSUE,
                f"🚨 edge-harness: {marker}\n"
                f"Суточный потолок автозаведения {SOFT_FAILURE_LABEL} задач "
                f"({SOFT_FAILURE_DAILY_CAP}) исчерпан — классы {sorted(cap_skipped)} НЕ "
                "завели задачу автоматически, нужен человек.\n\n"
                "Что дальше: посмотреть открытые задачи с меткой "
                f"{SOFT_FAILURE_LABEL} и шаг «Дайджест мягких отказов» в step summary "
                "последнего прогона orchestra, завести руками "
                "(scripts/gh/issue-create) или поднять SOFT_FAILURE_DAILY_CAP, "
                "если объём временный.")
    return observations, actions


def quota_sufficient(threshold: int = SOFT_FAILURE_QUOTA_THRESHOLD) -> tuple[bool, str]:
    """True — квоты GitHub API хватает НА ВЕСЬ полный скан (не только на
    гейт-проверку) — см. SOFT_FAILURE_QUOTA_THRESHOLD (находка ревью PR
    #1136, блокер 2: общий rate_guard.DEFAULT_THRESHOLD=300 калиброван под
    сам orchestra, не под смету этого скана в ~700-750 вызовов). Сбой
    ЧТЕНИЯ квоты — консервативно считается «недостаточно»: не начинать
    дорогой скан, не зная бюджета, лучше отложить до следующего тика."""
    try:
        core = rate_guard.fetch_core()
    except rate_guard.QuotaCheckFailed as error:
        return False, f"квота GitHub API не прочитана ({error}) — скан отложен консервативно"
    remaining, limit = core["remaining"], core["limit"]
    if rate_guard.should_skip(core, threshold):
        return False, (
            f"квота {remaining}/{limit} ниже порога {threshold} для полного "
            "скана — отложено до следующего тика (гейт digest_due не тронут "
            "heartbeat'ом, следующий тик попробует снова)")
    return True, f"квота {remaining}/{limit} — скан идёт"


def soft_failure_digest(repo: str, now: datetime) -> tuple[list[str], list[str], bool]:
    """Один полный цикл: гейт → квота → скан → таблица (в наблюдения,
    печатается caller'ом целиком) → эскалация над порогом → heartbeat.

    Третий элемент возврата — `ok`: False ТОЛЬКО на полном провале чтения
    (ни один из пяти workflow не прочитан, `collect_window`'s
    `workflows_ok == 0`) — находка ревью PR #1136, блокер 3: «скан не
    удался» и «скан удался, групп просто нет» раньше давали ОДИНАКОВЫЙ
    текст «Группы не найдены» и ОДИНАКОВЫЙ heartbeat, закрывающий гейт на
    `DIGEST_INTERVAL_HOURS` — ложь маскировала себя на 12 часов, именно
    тогда, когда #1100 (исчерпание квоты) делает такой провал вероятнее.
    Heartbeat при `ok=False` НЕ пишется — гейт остаётся открытым, следующий
    обычный тик (~15 мин) повторит попытку, пока падение дешёвое (один
    вызов метаданных #120 на несостоявшийся скан).

    `escalate_stale_scan` (находка ревью PR #1136, пятый круг, блокер 2)
    вызывается ПЕРВЫМ, ДО обоих ранних `return` ниже (рано / квоты не
    хватает) — иначе устойчивая нехватка квоты держала бы оба гейта в
    состоянии «пропускаю» бесконечно, и застой самого дайджеста никогда бы
    не сообщился."""
    stale_obs = escalate_stale_scan(repo, now)

    if not digest_due(repo, now):
        return stale_obs + ["⏭️ soft-failure-digest: рано — предыдущий полный скан моложе интервала"], [], True

    quota_ok, quota_text = quota_sufficient()
    if not quota_ok:
        return stale_obs + [f"⏭️ soft-failure-digest: {quota_text}"], [], True

    groups, observations, stats = collect_window(repo, now)
    observations = stale_obs + observations
    if stats["workflows_ok"] == 0:
        # Находка ревью PR #1136 (третий круг): текст с эмодзи-префиксом
        # 🚨 попадал только в наблюдения/step summary — НЕ становился
        # аннотацией Checks API (workflow command `::warning::` обязан
        # быть началом СТРОКИ, эмодзи ломает распознавание), а значит канал
        # A этого же дайджеста не мог поймать «наблюдатель не смог
        # посмотреть» на следующем скане — обещание спеки не выполнялось.
        # `::warning::` В НАЧАЛЕ строки — настоящая аннотация, отличимая по
        # тексту (не «Process completed with exit code N», как у мёртвого
        # шага) от группы continue-on-error orchestra, значит НЕ схлопнется
        # с ней дедупом.
        observations.append(
            f"::warning::soft-failure-digest: скан НЕ удался — прочитано "
            f"0/{stats['workflows_total']} workflow. Окно НЕ проверено — "
            "это не «группы не найдены» (группы не найдены значит «прочитано, "
            "пусто»; здесь — «не прочитано вовсе»). Heartbeat не пишется, "
            "следующий тик повторит попытку.")
        return observations, [], False

    table = render_digest_table(groups)
    observations = observations + [table]

    ranked = groups_over_threshold(groups)
    esc_obs, actions = escalate_groups(repo, ranked, now)
    observations += esc_obs

    # Находка ревью PR #1136 (пятый круг, блокер 1): «workflow целиком не
    # прочитан» и «workflow прочитан, но часть его прогонов — нет» — оба
    # обязаны попасть в единственное место, которое гейт цикла читает
    # гарантированно (heartbeat), а не рассыпаться ⚠️-строками step summary.
    unread = [w for w in DIGEST_WORKFLOWS if w not in stats["workflows_read"]]
    unread += [
        f"{w} (частично: {n} прогонов)"
        for w, n in sorted(stats["partial_reads_failed"].items()) if n
    ]
    try:
        mark_heartbeat(repo, now, unread=unread)
    except RuntimeError as error:
        observations.append(f"::warning::soft-failure-digest: heartbeat в #{WATCHDOG_ISSUE} не оставлен ({error})")

    return observations, actions, True


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    now = datetime.now(timezone.utc)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    observations, actions, ok = soft_failure_digest(repo, now)
    lines = ["## Дайджест мягких отказов (#1121)", ""] + observations + actions
    text = "\n".join(lines) + "\n"
    print(text)
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as file:
            file.write(text)
    # continue-on-error в orchestra.yml удержит job зелёным (находка ревью
    # PR #887/#896: аннотация Checks API переживает маскировку, канал A
    # этого же дайджеста поймает такой красный шаг на следующем скане —
    # ok=False не должно быть тихим (AGENTS.md, «fail loud, не silent-wrong»).
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
