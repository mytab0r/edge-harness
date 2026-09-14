#!/usr/bin/env python3
"""Сторож зомби-прогонов PR-чеков: `queued` с нулём job'ов, `gh run rerun`
отказывает, watchdog'а не было (issue #1106, живой случай PR #1088).

## Класс отказа — не «раннер не выдан» (#1115/#1176)

`GET /actions/runs/{id}/jobs` иногда отвечает `total_count: 0` НАВСЕГДА —
job'ы не материализовались вовсе, run остаётся `status=queued` без единого
job'а. Это НЕ класс «job создан, но раннер не выдан» (#1115/#1176 — там
`total_count > 0`, просто ждать нечего раннера): здесь ждать нечему в
принципе, `gh run rerun` отказывает («This workflow is already running»),
потому что run числится живым.

## Живой замер (2026-09-14, issue #1106)

`GET /actions/runs?event=pull_request&status=queued` на этом репозитории
вернул РОВНО 8 таких прогонов — все восемь required-чеков PR #1088 (head
`9a75c414`), созданы 2026-09-13T08:46:40Z, возраст на момент замера >22ч,
`total_count: 0` у КАЖДОГО. PR #1088 к моменту замера уже слит (близнец
обошёлся close→reopen руками) — эти восемь run-объектов остаются `queued`
НАВСЕГДА (подтверждено текстом issue: «старые зомби-прогоны остались queued
навсегда, job'ов не создают, чек-раннам не мешают») — это ключевой факт,
на котором строится дедуп ниже (см. «Рецидив после действия»).

Порог возраста `ZOMBIE_AGE_MINUTES=15` — НЕ выведен из серии инцидентов (её
нет), но обоснован измерением нормального пути: живой прогон 34811586423
(2026-09-14, repo-ci) создал первый job через **1 секунду** после
`created_at` самого run'а. 15 минут — ~900-кратный запас над нормой и
далеко ниже наблюдённого часа+ зависания #1088 — задержки такого порядка
GitHub нигде не гарантирует (docs/research/21-github-actions.md, «Задержка
старта — не документирована»), но и не наблюдались ни разу за всё время
существования конвейера.

## Газ — переэмиссия событий (close→reopen), не бесконечный retry

Единственный проверенный живым случаем газ — `gh pr close && gh pr reopen`:
head SHA не меняется, все обязательные workflow получают `reopened` (входит
в дефолтный набор типов `pull_request`, явно перечислен там, где типы
сужены — проверено по всем 8 required workflow этого репозитория). Работает
ТОЛЬКО для ОТКРЫТОГО PR с прогоном на его ТЕКУЩЕМ head — устаревший прогон
(ветка ушла вперёд) и прогон уже закрытого/слитого PR не блокируют ничего
и не трогаются.

Действие — РОВНО один раз на `head_sha` (маркер `[zombie-run-watch:
переэмиссия <sha8>]` в теле комментария PR, дедуп через
`pulse_guard.issue_marker_times`): новый push даёт новый `head_sha` и,
значит, свежий бюджет попытки. Второй раз душить тем же способом нельзя —
не размножать close/reopen циклами без диагностики (AGENTS.md, «Тормоз без
газа не принимается»: автоматический газ пробуется один раз, дальше решение
переходит человеку явной эскалацией).

Порядок действия (находка ревью PR #1212): сначала close→reopen, маркер-отчёт —
ТОЛЬКО после успеха (он и есть честный отчёт о сделанном). Маркер, записанный
до действия, при отказе посередине навсегда глушит газ: следующий пульс читает
маркер, решает «переэмиссия уже была» и больше не пытается — попытка
потрачена, действия не было. Формы отказа различимы и обрабатываются по-разному:

- close не прошёл — состояние PR не менялось, газа не было, маркер не
  пишется: следующий пульс повторит попытку (⚠️ в observations);
- close прошёл, reopen не прошёл — PR ОСТАЛСЯ ЗАКРЫТЫМ, состояние хуже
  исходного: сторож смотрит только открытые PR и сам к нему не вернётся —
  эскалация в #120 немедленно, БЕЗ дедупа (каждый такой случай — новый
  инцидент, оставивший PR закрытым; повторное «уже эскалировано» утаило бы
  его от человека); если эскалация не доставлена ни одним каналом —
  красный прогон;
- газ применён, отчёт не записан — дедуп-якоря в PR нет, поэтому тот же
  маркер переэмиссии дублируется эскалацией в #120 (запасной носитель):
  следующий пульс, не найдя маркера в PR, читает его из #120 и повторной
  переэмиссии на этом head не делает — контракт «одна попытка на head_sha»
  держится на обоих носителях. Проверяется ОБА предиката доставки эскалации
  (`escalation_channel_failed` — оба канала молчат, `escalation_
  dedup_carrier_failed` — след в #120 не оставлен даже при доставленном
  Telegram): пропуск второго (находка ревью PR #1212, круг 3) означал бы, что
  маркер не найден НИГДЕ, а прогон всё равно зелёный.

## Токен: close/reopen под PAT, маркеры — под github.token (находка ревью PR #1212, круг 3)

`.github/workflows/zombie-run-watch.yml` даёт этому скрипту ДВЕ переменные:
`GH_TOKEN=github.token` (по умолчанию, для всех вызовов через `gh()`) и
`GH_PIPELINE_PAT` (секрет, читается `set_pr_state()` напрямую). Раздельно не
случайно: события, порождённые `GITHUB_TOKEN`, НЕ зажигают новые workflow-
прогоны (docs/research/21-github-actions.md, класс #18/#929/#955, тот же
класс, что #929/#955 — мерж под `GITHUB_TOKEN` не создавал push-событие) —
под `github.token` close→reopen были бы no-op для required-чеков, PR
оставался бы зомби НАВСЕГДА при сплошь зелёных прогонах сторожа. Только
`set_pr_state()` (PATCH close/open) читает `GH_PIPELINE_PAT` напрямую и идёт
сырым `subprocess` с заголовком `Authorization` (тот же приём, что
`scheduler.update_branch` с `ORCHESTRA_PAT`). Маркер-комментарий,
эскалация и ЧТЕНИЕ маркеров дедупа намеренно остаются под `github.token`
(`github-actions[bot]`): `trusted_login=EVENT_ACTOR_LOGIN` ищет маркеры
именно от этой identity — переведи их на PAT, и фильтр перестанет находить
свои же маркеры (они писались бы от лица владельца PAT), дедуп «одна
попытка на head_sha» сломался бы тихо и навсегда.

Чтение маркеров фильтрует автора (`trusted_login=EVENT_ACTOR_LOGIN`,
#1027): маркеры пишет только job под `github.token` (вне Actions запись
закрыта гейтом #1074), поэтому частичная цитата маркера в чужом ответе
(человек, агент, пересказ) дедуп не создаёт.

## Рецидив после действия — не путать со старым permanent-зомби

Живой факт выше («старые зомби остаются queued навсегда») означает, что ПОСЛЕ
успешной переэмиссии те же самые 8 старых run-ID останутся `queued` со
`total_count: 0` бесконечно, даже если новые прогоны (созданные `reopened`-
событием) прошли нормально. Наивная проверка «PR #N всё ещё зомби?» на
следующем пульсе увидела бы те же старые run-ID и ложно закричала бы
«автогаз не сработал» — этого не должно происходить. Различение: после
первой переэмиссии на рецидив проверяются ТОЛЬКО прогоны, созданные ПОЗЖЕ
момента переэмиссии (`runs_created_after`) — старые permanent-зомби тем
самым не участвуют в решении о рецидиве вообще, независимо от того, сколько
пульсов пройдёт.

## Цена

Steady state (нет queued PR-прогонов старше порога) — 1 вызов `gh api` за
пульс (список queued PR-прогонов). Кандидаты появляются — сверх того:

- +1 вызов: список открытых PR;
- до `MAX_ZOMBIE_CANDIDATES_PER_PULSE` вызовов числа job'ов (по одному на
  проверяемый прогон, с коротким замыканием: одного подтверждённого зомби
  на PR достаточно). Проверяются СТАРЕЙШИЕ кандидаты: `/actions/runs`
  отдаёт список от свежих к старым (замер 2026-09-14: `created_at`
  убывает с первой записи), усечение берёт хвост списка — при затяжном
  сбое (>20 кандидатов пульс за пульсом) старейшие, самые критичные
  зомби обслуживаются первыми, а не вытесняются свежими;
- +1 вызов чтения маркеров в комментариях КАЖДОГО PR-кандидата (одна
  страница комментариев целиком; на длинной истории — несколько);
- +1 вызов чтения маркеров в #120 на каждый PR-кандидат, чей собственный
  носитель маркера пуст (запасной носитель случая «отчёт не записан»);
- только в путях рецидива/отказа: ещё чтение маркера эскалации в #120 и
  запись эскалации (комментарий в #120 + Telegram).

Верхняя граница обычного пульса с кандидатами — около
`2 + MAX_ZOMBIE_CANDIDATES_PER_PULSE + 2·<число PR-кандидатов>` вызовов
`gh api` (находка ревью PR #1212: первая версия считала только
`2 + MAX_ZOMBIE_CANDIDATES_PER_PULSE` и не включала чтения маркеров),
независимо от того, сколько PR реально открыто.

Запуск: python scripts/orchestra/zombie_run_watch.py run
Тесты:  python -m pytest scripts/orchestra/test_zombie_run_watch.py -q
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
import os
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent))  # pulse_guard сосед

from pulse_guard import (  # noqa: E402
    EVENT_ACTOR_LOGIN,
    WATCHDOG_ISSUE,
    escalate,
    escalation_channel_failed,
    escalation_dedup_carrier_failed,
    gh,
    issue_marker_times,
    minutes_between,
    parse_time,
    post_issue_comment,
)

_LIB = Path(__file__).resolve().parents[1] / "lib"
_RL_SPEC = importlib.util.spec_from_file_location("review_labels", _LIB / "review_labels.py")
review_labels = importlib.util.module_from_spec(_RL_SPEC)
_RL_SPEC.loader.exec_module(review_labels)  # type: ignore[union-attr]


# ── Пороги (обоснование — докстринг модуля) ─────────────────────────────────

ZOMBIE_AGE_MINUTES = 15
MAX_ZOMBIE_CANDIDATES_PER_PULSE = 20

REOPENED_MARKER_PREFIX = "[zombie-run-watch: переэмиссия "
ESCALATION_MARKER_PREFIX = "[zombie-run-watch: эскалация "
REOPEN_FAILED_MARKER_PREFIX = "[zombie-run-watch: reopen-не-удался "
REPORT_FAILED_MARKER_PREFIX = "[zombie-run-watch: отчёт-не-записан "


# ── Чистая логика (тестируется без сети) ────────────────────────────────────


def stale_candidates(runs: list[dict], now: datetime,
                     age_minutes: int = ZOMBIE_AGE_MINUTES) -> list[dict]:
    """Прогоны из уже отфильтрованного (`status=queued&event=pull_request`)
    списка, чей возраст по `created_at` >= порога — дорогой запрос числа
    job'ов делаем только для них."""
    return [
        r for r in runs
        if r.get("status") == "queued"
        and minutes_between(parse_time(r["created_at"]), now) >= age_minutes
    ]


def runs_created_after(runs: list[dict], after: datetime) -> list[dict]:
    """Подмножество `runs`, созданных СТРОГО позже `after` — отсекает старые
    permanent-зомби от решения о рецидиве (см. докстринг модуля, «Рецидив
    после действия»)."""
    return [r for r in runs if parse_time(r["created_at"]) > after]


def open_prs_by_branch(prs: list[dict]) -> dict[str, dict]:
    """`head.ref` -> объект PR, только записи с валидной формой `head`."""
    result: dict[str, dict] = {}
    for pr in prs:
        head = pr.get("head")
        if isinstance(head, dict) and isinstance(head.get("ref"), str):
            result[head["ref"]] = pr
    return result


def group_candidates_by_pr(candidates: list[dict], prs_by_branch: dict[str, dict]) -> dict[int, dict]:
    """номер PR -> {"pr": объект, "runs": [...]} — только кандидаты, чей
    `head_branch` совпадает с ОТКРЫТЫМ PR и чей `head_sha` совпадает с
    ТЕКУЩИМ head этого PR (иначе прогон устарел — ветка ушла вперёд, этот
    конкретный чек уже не блокирует слияние, независимо от его статуса)."""
    grouped: dict[int, dict] = {}
    for run in candidates:
        pr = prs_by_branch.get(run.get("head_branch"))
        if pr is None:
            continue
        current_sha = (pr.get("head") or {}).get("sha")
        if not current_sha or run.get("head_sha") != current_sha:
            continue
        number = pr.get("number")
        if not isinstance(number, int):
            continue
        entry = grouped.setdefault(number, {"pr": pr, "runs": []})
        entry["runs"].append(run)
    return grouped


def reopen_comment_body(run_ids: list[int], sha8: str, marker: str) -> str:
    ids_text = ", ".join(str(i) for i in run_ids)
    return (
        f"{marker}\n\n"
        f"Обнаружен зомби-прогон (`queued`, 0 job'ов, старше {ZOMBIE_AGE_MINUTES} мин): "
        f"run {ids_text} на head `{sha8}`. Класс #1106: `gh run rerun` не работает "
        "(«This workflow is already running» — нечего перезапускать), газ — переэмиссия "
        "событий PR (close→reopen, head SHA не меняется) — тем же способом обошли живой "
        "случай #1088.\n\n"
        "Действие: PR автоматически закрыт и переоткрыт этим сторожем."
    )


def escalation_text(pr_number: int, run_ids: list[int], sha8: str, marker: str) -> str:
    ids_text = ", ".join(str(i) for i in run_ids)
    return (
        f"🚨 edge-harness: {marker} PR #{pr_number} остаётся зомби (queued, 0 job'ов, "
        f"run {ids_text}) НА ТОМ ЖЕ head `{sha8}` уже ПОСЛЕ автоматической переэмиссии — "
        "автогаз не сработал (или зомби-состояние повторилось), повторно закрывать/"
        "открывать автоматически не буду (issue #1106: одна попытка на head_sha). "
        "Нужен человек."
    )


def reopen_failed_text(pr_number: int, run_ids: list[int], sha8: str,
                       marker: str, error: str) -> str:
    """Случай «close прошёл, reopen не прошёл» (находка ревью PR #1212):
    PR остался закрыт — факт, а не гипотеза, текст называет его прямо."""
    ids_text = ", ".join(str(i) for i in run_ids)
    return (
        f"🚨 edge-harness: {marker} Сторож зомби-прогонов (#1106) закрыл PR #{pr_number} "
        f"для переэмиссии событий (run {ids_text}, head `{sha8}`), но переоткрыть не "
        f"удалось: {error}. PR ОСТАЛСЯ ЗАКРЫТЫМ — сторож смотрит только открытые PR и "
        "сам к нему не вернётся. Нужен человек: переоткрыть PR и разобрать причину "
        "отказа reopen."
    )


def report_failed_text(pr_number: int, run_ids: list[int], sha8: str,
                       reopened_marker: str, marker: str, error: str) -> str:
    """Случай «газ применён, отчёт не записан»: текст несёт САМ маркер
    переэмиссии — этот комментарий в #120 становится запасным носителем
    дедупа, который следующий пульс читает, когда в PR маркера нет."""
    ids_text = ", ".join(str(i) for i in run_ids)
    return (
        f"🚨 edge-harness: {marker} Переэмиссия событий PR #{pr_number} (close→reopen, "
        f"#1106, {reopened_marker}) ВЫПОЛНЕНА, но комментарий-отчёт с этим дедуп-маркером "
        f"в PR записать не удалось: {error}. Дедуп «одна попытка на head_sha» живёт "
        "только здесь — последующие пульсы читают маркер из #120 и повторной "
        f"переэмиссии на этом head (`{sha8}`) не делают. run {ids_text}."
    )


# ── I/O ─────────────────────────────────────────────────────────────────


def queued_pr_runs(repo: str, per_page: int = 100) -> list[dict]:
    """Все PR-триггернутые прогоны в `queued` по репозиторию целиком, ОДНИМ
    вызовом (замер 2026-09-14, issue #1106: восемь зомби-прогонов историчес-
    кого инцидента отвечают этим же запросом одним вызовом). Не-list форма
    внутри `workflow_runs` — громкий сбой (класс #120A), не «прогонов нет»;
    полная страница — тоже громкий сбой (класс #308, хвост не дочитан)."""
    payload = gh(f"repos/{repo}/actions/runs?event=pull_request&status=queued&per_page={per_page}")
    runs = payload.get("workflow_runs") if isinstance(payload, dict) else None
    if not isinstance(runs, list):
        raise RuntimeError(
            f"zombie-run-watch: /actions/runs?status=queued ответил не ожидаемой формой "
            f"({type(payload).__name__}) — не 'прогонов нет', форма ответа неверна")
    if len(runs) >= per_page:
        raise RuntimeError(
            f"zombie-run-watch: /actions/runs?status=queued вернул полную страницу "
            f"({per_page}) — хвост списка этим запросом недочитан (класс #308)")
    return runs


def jobs_total_count(repo: str, run_id: int) -> int:
    payload = gh(f"repos/{repo}/actions/runs/{run_id}/jobs")
    if not isinstance(payload, dict) or "total_count" not in payload:
        raise RuntimeError(
            f"zombie-run-watch: /actions/runs/{run_id}/jobs — не ожидаемая форма ответа "
            f"({type(payload).__name__})")
    return int(payload.get("total_count") or 0)


def is_zombie(repo: str, run: dict) -> bool:
    return jobs_total_count(repo, run["id"]) == 0


def set_pr_state(repo: str, number: int, state: str) -> None:
    """Примитив газа (issue #1106, PR #1088): close и reopen выполняются
    РАЗДЕЛЬНЫМИ вызовами не зря — отказ reopen после успешного close
    оставляет PR закрытым, и вызывающий обязан различить эти два отказа
    (находка ревью PR #1212, круг 2: один catch-all над парой склеивал их в
    «переэмиссия не выполнена»).

    PAT владельца (`GH_PIPELINE_PAT`), не `github.token` (находка ревью
    PR #1212, круг 3): события от GITHUB_TOKEN не зажигают новые workflow-
    прогоны (docs/research/21-github-actions.md, класс #18/#929/#955) — под
    github.token этот PATCH был бы no-op для required-чеков, PR оставался бы
    зомби НАВСЕГДА при сплошь зелёных прогонах сторожа. Сырой `subprocess` с
    заголовком `Authorization` — тот же приём, что `scheduler.update_branch`
    с `ORCHESTRA_PAT`, НЕ через `gh()` (тот читает `GH_TOKEN` из окружения,
    которым для ОСТАЛЬНЫХ вызовов этого модуля намеренно остаётся
    `github.token`/`github-actions[bot]`: маркер-комментарий и чтение
    маркеров дедупа обязаны остаться под этой же identity, иначе
    `trusted_login=EVENT_ACTOR_LOGIN` перестанет находить свои же маркеры —
    они писались бы от лица владельца PAT, и дедуп «одна попытка на
    head_sha» сломается тихо и навсегда). Без PAT в окружении (локальный
    прогон/тест) — честный fallback на `gh()`/`GH_TOKEN`, как у
    `update_branch`."""
    pat = os.environ.get("GH_PIPELINE_PAT")
    if not pat:
        gh("-X", "PATCH", f"repos/{repo}/pulls/{number}", "-f", f"state={state}")
        return
    result = subprocess.run(
        ["gh", "api", "-X", "PATCH", f"repos/{repo}/pulls/{number}",
         "-f", f"state={state}", "-H", f"Authorization: Bearer {pat}"],
        capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "NO_COLOR": "1"},
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"gh api -X PATCH repos/{repo}/pulls/{number} state={state} (PAT): "
            f"{result.stderr.strip()}")


def zombie_run_watch(repo: str, now: datetime) -> tuple[list[str], list[str]]:
    """Один пульс: найти застрявшие queued-прогоны required-чеков на ТЕКУЩЕМ
    head открытых PR, один раз на head_sha переэмиттить события, на рецидив
    после переэмиссии — эскалация (не повторный retry). См. докстринг модуля
    целиком за обоснованием порогов и различением от permanent-зомби."""
    observations: list[str] = []
    actions: list[str] = []

    runs = queued_pr_runs(repo)
    candidates = stale_candidates(runs, now)
    if not candidates:
        observations.append(
            f"zombie-run-watch: queued PR-прогонов старше {ZOMBIE_AGE_MINUTES} мин нет "
            f"(всего queued PR-прогонов: {len(runs)})")
        return observations, actions

    if len(candidates) > MAX_ZOMBIE_CANDIDATES_PER_PULSE:
        observations.append(
            f"⚠️ zombie-run-watch: кандидатов {len(candidates)}, проверяются старейшие "
            f"{MAX_ZOMBIE_CANDIDATES_PER_PULSE} (потолок цены API за один пульс; "
            "API отдаёт от свежих к старым — берём хвост)")
        candidates = candidates[-MAX_ZOMBIE_CANDIDATES_PER_PULSE:]

    prs = review_labels.list_pages(f"repos/{repo}/pulls?state=open&per_page=100", gh)
    prs_by_branch = open_prs_by_branch(prs)
    grouped = group_candidates_by_pr(candidates, prs_by_branch)

    if not grouped:
        observations.append(
            f"zombie-run-watch: {len(candidates)} застрявших queued-прогон(ов) старше "
            f"{ZOMBIE_AGE_MINUTES} мин, ни один не относится к текущему head открытого PR "
            "(устарели или PR уже закрыт/слит) — действие не требуется")
        return observations, actions

    for pr_number, entry in sorted(grouped.items()):
        pr = entry["pr"]
        head_sha = (pr.get("head") or {}).get("sha") or ""
        sha8 = head_sha[:8]
        reopened_marker = f"{REOPENED_MARKER_PREFIX}{sha8}]"
        escalation_marker = f"{ESCALATION_MARKER_PREFIX}PR#{pr_number}@{sha8}]"

        try:
            reopen_times = issue_marker_times(repo, pr_number, reopened_marker,
                                              trusted_login=EVENT_ACTOR_LOGIN)
        except RuntimeError as error:
            observations.append(
                f"⚠️ zombie-run-watch: PR #{pr_number} — маркеры не прочитаны ({error}), "
                "действие в этом пульсе пропущено (не гадаем, применялось ли оно уже)")
            continue

        if not reopen_times:
            # PR-носитель пуст — проверить запасной носитель в #120 (случай
            # «газ применён, отчёт в PR не записан», см. докстринг модуля:
            # эскалация report_failed_text несёт тот же маркер переэмиссии).
            try:
                reopen_times = issue_marker_times(repo, WATCHDOG_ISSUE, reopened_marker,
                                                  trusted_login=EVENT_ACTOR_LOGIN)
            except RuntimeError as error:
                observations.append(
                    f"⚠️ zombie-run-watch: PR #{pr_number} — запасные маркеры "
                    f"#{WATCHDOG_ISSUE} не прочитаны ({error}), действие в этом "
                    "пульсе пропущено")
                continue

        if reopen_times:
            last_reopen_at = max(reopen_times)
            runs_to_check = runs_created_after(entry["runs"], last_reopen_at)
            if not runs_to_check:
                observations.append(
                    f"zombie-run-watch: PR #{pr_number} — известный старый зомби-прогон "
                    f"(переэмиссия уже была {last_reopen_at.isoformat()}), новых зомби "
                    "после неё нет — не трогаю")
                continue
        else:
            runs_to_check = entry["runs"]

        try:
            zombie_run = next((r for r in runs_to_check if is_zombie(repo, r)), None)
        except RuntimeError as error:
            observations.append(
                f"⚠️ zombie-run-watch: PR #{pr_number} — число job'ов не прочитано "
                f"({error}), этот PR пропущен в этом пульсе")
            continue

        if zombie_run is None:
            continue  # прогоны ещё не старше по job'ам — просто медленный старт, не зомби

        run_ids = [zombie_run["id"]]

        if not reopen_times:
            # Находка ревью PR #1212: действие ПЕРВЫМ, маркер-отчёт — только
            # после успеха (он и есть честный отчёт о сделанном). Три формы
            # отказа различимы и обрабатываются по-разному (докстринг
            # модуля, «Порядок действия»).
            try:
                set_pr_state(repo, pr_number, "closed")
            except RuntimeError as error:
                # (а) close не прошёл — состояние PR не менялось, газа не
                # было, маркер не пишется: следующий пульс повторит попытку.
                observations.append(
                    f"⚠️ zombie-run-watch: PR #{pr_number} — закрытие не выполнено "
                    f"({error}), маркер не пишется, попытка повторится в следующем пульсе")
                continue
            try:
                set_pr_state(repo, pr_number, "open")
            except RuntimeError as error:
                # (б) close прошёл, reopen не прошёл — PR ОСТАЛСЯ ЗАКРЫТЫМ,
                # состояние хуже исходного: сторож смотрит только открытые
                # PR и сам к нему не вернётся. Эскалация немедленно и БЕЗ
                # дедупа: каждый такой случай — новый инцидент, оставивший
                # PR закрытым, «уже эскалировано» утаило бы его от человека.
                marker = f"{REOPEN_FAILED_MARKER_PREFIX}PR#{pr_number}@{sha8}]"
                text = reopen_failed_text(pr_number, run_ids, sha8, marker, error)
                esc_result = escalate(repo, WATCHDOG_ISSUE, text)
                actions.append(
                    f"🚨 zombie-run-watch: PR #{pr_number} ЗАКРЫТ без переоткрытия "
                    f"(run {run_ids[0]}, head {sha8}) — эскалация #{WATCHDOG_ISSUE}: "
                    f"{esc_result}")
                if escalation_channel_failed(esc_result):
                    raise RuntimeError(
                        f"zombie-run-watch: PR #{pr_number} остался ЗАКРЫТЫМ (close "
                        f"прошёл, reopen не прошёл: {error}), эскалация не доставлена "
                        "ни одним каналом — красный прогон вместо молчаливого закрытого PR")
                continue
            try:
                post_issue_comment(repo, pr_number,
                                   reopen_comment_body(run_ids, sha8, reopened_marker))
            except RuntimeError as error:
                # (в) газ ПРИМЕНЁН, отчёт и дедуп-якорь в PR не записаны.
                # Повтор газа вслепую — нарушение контракта «одна попытка на
                # head_sha», поэтому тот же маркер переэмиссии уходит
                # эскалацией в #120: он становится запасным носителем дедупа,
                # который следующий пульс читает (см. ветку выше).
                marker = f"{REPORT_FAILED_MARKER_PREFIX}PR#{pr_number}@{sha8}]"
                text = report_failed_text(pr_number, run_ids, sha8,
                                          reopened_marker, marker, error)
                esc_result = escalate(repo, WATCHDOG_ISSUE, text)
                actions.append(
                    f"⚠️🔁 zombie-run-watch: PR #{pr_number} переэмиссия ПРИМЕНЕНА "
                    f"(run {run_ids[0]}, head {sha8}), отчёт в PR не записан — "
                    f"дедуп-якорь в #{WATCHDOG_ISSUE}: {esc_result}")
                # Находка ревью PR #1212 (круг 3): проверять ОБА предиката, не
                # только «оба канала молчат». Здесь важен именно
                # escalation_dedup_carrier_failed — комментарий в #120 это
                # запасной носитель дедупа (report_failed_text несёт тот же
                # маркер переэмиссии), и если он не записан, следующий пульс
                # не найдёт маркер НИГДЕ (ни в PR, ни в #120) и повторит
                # переэмиссию вслепую — даже если Telegram сам алерт доставил.
                if escalation_channel_failed(esc_result) or escalation_dedup_carrier_failed(esc_result):
                    raise RuntimeError(
                        f"zombie-run-watch: PR #{pr_number} переэмиссия применена, "
                        f"но отчёт-дедуп не записан ни в PR ({error}), ни в "
                        f"#{WATCHDOG_ISSUE} — следующий пульс может повторить "
                        "переэмиссию, не зная о этой (красный прогон вместо повтора газа)")
                continue
            actions.append(
                f"🔁 zombie-run-watch: PR #{pr_number} переоткрыт (run {run_ids[0]}, head {sha8})")
        else:
            try:
                already_escalated = bool(issue_marker_times(
                    repo, WATCHDOG_ISSUE, escalation_marker,
                    trusted_login=EVENT_ACTOR_LOGIN))
            except RuntimeError as error:
                observations.append(
                    f"⚠️ zombie-run-watch: PR #{pr_number} — маркеры {WATCHDOG_ISSUE} не "
                    f"прочитаны ({error}), эскалация в этом пульсе пропущена")
                continue
            if already_escalated:
                observations.append(
                    f"zombie-run-watch: PR #{pr_number} — рецидив уже эскалирован, не повторяю")
                continue
            text = escalation_text(pr_number, run_ids, sha8, escalation_marker)
            esc_result = escalate(repo, WATCHDOG_ISSUE, text)
            actions.append(
                f"🚨 zombie-run-watch: PR #{pr_number} — рецидив после переэмиссии, "
                f"эскалация #{WATCHDOG_ISSUE}: {esc_result}")
            # Находка ревью PR #1212 (круг 3): результат escalate() здесь
            # клался в actions и терялся молча — при отказе обоих каналов
            # (Telegram и след в #120) сигнал «автогаз не сработал, нужен
            # человек» не доходил никуда, а прогон оставался зелёным.
            if escalation_channel_failed(esc_result):
                raise RuntimeError(
                    f"zombie-run-watch: PR #{pr_number} — рецидив после переэмиссии, "
                    f"эскалация #{WATCHDOG_ISSUE} не доставлена ни одним каналом "
                    f"({esc_result}) — красный прогон вместо молчаливого зомби")

    if not observations and not actions:
        observations.append("zombie-run-watch: кандидатов не было")
    return observations, actions


# ── CLI ────────────────────────────────────────────────────────────────────


def cmd_run(_args: argparse.Namespace) -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    now = datetime.now(timezone.utc)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    try:
        observations, actions = zombie_run_watch(repo, now)
    except RuntimeError as error:
        # Fail loud (AGENTS.md, «Проверяй видимый результат, а не шаг» +
        # третье состояние #1096): не смог посмотреть — красный прогон, не
        # тихое «зомби-прогонов нет».
        text = f"🚨 zombie-run-watch: прогоны не прочитаны ({error}). Прогон красный (fail loud)."
        print(text, file=sys.stderr)
        if summary_path:
            with open(summary_path, "a", encoding="utf-8") as file:
                file.write("## zombie-run-watch\n\n" + text + "\n")
        return 1
    lines = ["## zombie-run-watch", ""] + observations + actions
    text = "\n".join(lines) + "\n"
    print(text)
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as file:
            file.write(text)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="снять queued PR-прогоны, переэмиссия зомби (дедуп по head_sha)")
    args = parser.parse_args()
    return {"run": cmd_run}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
