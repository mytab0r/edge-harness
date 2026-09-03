#!/usr/bin/env python3
"""Сборщик недельного дайджеста (#116): закрытые задачи/PR за период из GitHub,
состояние журнала — и доставка в каналы конфига автоматизации.

Сквозной пример владельца: раз в неделю постить дайджест проделанной работы в
Slack и класть черновик в Telegram. Исполнение — раннер (не DO: 10 ms CPU,
30-rejected п.6); вызов — job автоматизации (repository_dispatch
harness-automation). Правила:

- каналы изолированы: отказ Slack не мешает Telegram; итог честный
  (ok=false, каналы с деталями в result-файле) — отказ виден;
- секреты каналов приходят env (SLACK_BOT_TOKEN, TELEGRAM_BOT_TOKEN,
  TELEGRAM_CHAT_ID) — значений в конфиге автоматизации нет и быть не может;
- тесты кормятся прод-формой ответов GitHub/Slack/Telegram
  (scripts/automations/test_digest.py), не пересказом.

Выход: 0 — все настроенные каналы доставили; 1 — есть отказ (job красный,
run.sh пишет job_end fail). Result-JSON: см. build_result().
"""

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

HTTP_TIMEOUT_SECS = 15
MAX_ITEMS_IN_DIGEST = 10
DIGEST_PAGES_MAX = 3
TELEGRAM_TEXT_LIMIT = 4000  # лимит Telegram 4096 символов — с запасом

# ── Тонкая проводка: GitHub и HTTP (подменяется в тестах) ──────────────────────────


def gh_api(query: str) -> dict:
    """gh api с query-строкой: аутентификация раннера — GITHUB_TOKEN job'а."""
    result = subprocess.run(
        ["gh", "api", query],
        capture_output=True, text=True,
        env={**os.environ, "NO_COLOR": "1"},
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh api не удался: {result.stderr.strip()[:300]}")
    return json.loads(result.stdout)


def post_json(url: str, headers: dict, payload: dict) -> tuple[int, dict]:
    body = json.dumps(payload).encode()
    request = urllib.request.Request(url, data=body, method="POST", headers={
        **headers, "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECS) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        # Прод-форма отказа канала — тоже JSON: читаем, не выбрасываем.
        try:
            return error.code, json.loads(error.read().decode())
        except (ValueError, OSError):
            return error.code, {}
    except (urllib.error.URLError, OSError) as error:
        raise RuntimeError(f"сеть недоступна: {error}") from error


# ── Сбор: GitHub search API и журнал ────────────────────────────────────────────────


def search_github(repo: str, qualifier: str, date_from: dt.date, date_to: dt.date) -> list[dict]:
    """Поиск по датам (search API: closed:/merged: принимают диапазон d1..d2).
    Пагинация по page до исчерпания выборки, потолок DIGEST_PAGES_MAX страниц
    по 100 — дайджесту еженедельного репозитория хватает с избытком."""
    query = urllib.parse.quote(
        f"repo:{repo} {qualifier}:{date_from.isoformat()}..{date_to.isoformat()}")
    items: list[dict] = []
    for page in range(1, DIGEST_PAGES_MAX + 1):
        payload = gh_api(f"search/issues?q={query}&per_page=100&page={page}")
        if not isinstance(payload.get("items"), list):
            raise RuntimeError("ответ search/issues без items — не по прод-форме")
        items.extend(payload["items"])
        if len(payload["items"]) < 100:
            break
    return items


def collect_issues_and_prs(repo: str, date_from: dt.date, date_to: dt.date) -> tuple[list[dict], list[dict]]:
    """Закрытые issue (без pull_request — он есть только у PR) и слитые PR."""
    raw = search_github(repo, "is:issue is:closed", date_from, date_to)
    issues = [
        {"number": item["number"], "title": item["title"], "url": item["html_url"]}
        for item in raw if "pull_request" not in item
    ]
    raw = search_github(repo, "is:pr is:merged", date_from, date_to)
    pulls = [
        {"number": item["number"], "title": item["title"], "url": item["html_url"]}
        for item in raw if "pull_request" in item
    ]
    return issues, pulls


def collect_journal(hand_url: str, hand_token: str, since_ts: int, until_ts: int) -> dict:
    """Счётчики журнала за период из прод-формы GET /api/tasks:
    {tasks: [{id, created_ts, dispatch_ts, latency_ms, status}]}.
    Журнал хранит последние 100 задач (LIMITS.tasksListMax) — для недельного
    дайджеста этого достаточно; больше честно не обещаем."""
    if not hand_url or not hand_token:
        raise RuntimeError("HANDS_URL/HANDS_TOKEN не заданы — журнал недоступен")
    request = urllib.request.Request(
        hand_url.rstrip("/") + "/api/tasks",
        headers={"Authorization": f"Bearer {hand_token}"},
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECS) as response:
        payload = json.loads(response.read().decode())
    counts: dict[str, int] = {}
    automation_runs = 0
    for task in payload.get("tasks", []):
        created = task.get("created_ts")
        if not isinstance(created, int) or not since_ts <= created <= until_ts:
            continue
        if str(task.get("id", "")).startswith("automation:"):
            automation_runs += 1
        status = str(task.get("status", "unknown"))
        counts[status] = counts.get(status, 0) + 1
    return {"counts": counts, "automation_runs": automation_runs}


# ── Формат и доставка ───────────────────────────────────────────────────────────────


def format_digest(repo: str, date_from: dt.date, date_to: dt.date,
                  issues: list[dict], pulls: list[dict], journal: dict) -> str:
    lines = [
        f"Дайджест `{repo}` за {date_from.isoformat()} — {date_to.isoformat()}",
        "",
        f"Закрытые задачи: {len(issues)}; слитые PR: {len(pulls)}.",
    ]
    if journal["counts"]:
        counts = ", ".join(f"{status}: {n}" for status, n in sorted(journal["counts"].items()))
        lines.append(f"Журнал (прогонов задач: {journal['automation_runs']}): {counts}.")
    if issues:
        lines.append("")
        lines.append("Задачи:")
        lines += [f"- #{i['number']} {i['title']}" for i in issues[:MAX_ITEMS_IN_DIGEST]]
        if len(issues) > MAX_ITEMS_IN_DIGEST:
            lines.append(f"- … и ещё {len(issues) - MAX_ITEMS_IN_DIGEST}")
    if pulls:
        lines.append("")
        lines.append("Pull request'ы:")
        lines += [f"- #{p['number']} {p['title']}" for p in pulls[:MAX_ITEMS_IN_DIGEST]]
        if len(pulls) > MAX_ITEMS_IN_DIGEST:
            lines.append(f"- … и ещё {len(pulls) - MAX_ITEMS_IN_DIGEST}")
    lines.append("")
    lines.append(f"https://github.com/{repo}")
    return "\n".join(lines)


def deliver_slack(token: str, target: str, text: str) -> tuple[bool, str]:
    if not token:
        return False, "SLACK_BOT_TOKEN не задан"
    status, payload = post_json(
        "https://slack.com/api/chat.postMessage",
        {"Authorization": f"Bearer {token}"},
        {"channel": target, "text": text},
    )
    # Прод-форма Slack: HTTP 200 всегда у Web API, успех несёт ok:true.
    if status != 200:
        return False, f"HTTP {status}"
    if payload.get("ok") is True:
        return True, f"доставлено (ts {payload.get('ts', '?')})"
    return False, f"slack: {payload.get('error', 'ответ без ok:true')}"


def deliver_telegram(token: str, chat_id: str, text: str) -> tuple[bool, str]:
    if not token or not chat_id:
        return False, "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID не заданы"
    if len(text) > TELEGRAM_TEXT_LIMIT:
        text = text[:TELEGRAM_TEXT_LIMIT] + "\n…(обрезано — лимит Telegram)"
    status, payload = post_json(
        f"https://api.telegram.org/bot{token}/sendMessage",
        {},
        {"chat_id": chat_id, "text": text},
    )
    if status != 200:
        detail = payload.get("description") or f"HTTP {status}"
        return False, f"telegram: {detail}"
    if payload.get("ok") is True:
        return True, "доставлено"
    return False, f"telegram: {payload.get('description', 'ответ без ok:true')}"


def deliver_all(channels: list[dict], text: str) -> list[dict]:
    """Каждый канал — отдельно: отказ одного не мешает остальным (критерий #116)."""
    results = []
    for channel in channels:
        kind = channel.get("type")
        try:
            if kind == "slack":
                ok, detail = deliver_slack(os.environ.get("SLACK_BOT_TOKEN", ""),
                                           str(channel.get("target", "")), text)
            elif kind == "telegram":
                ok, detail = deliver_telegram(os.environ.get("TELEGRAM_BOT_TOKEN", ""),
                                              os.environ.get("TELEGRAM_CHAT_ID", ""), text)
            else:
                ok, detail = False, f"неизвестный канал {kind}"
        except RuntimeError as error:
            ok, detail = False, str(error)
        results.append({"type": kind, "target": channel.get("target", ""), "ok": ok, "detail": detail})
    return results


def build_result(issues: list[dict], pulls: list[dict], journal: dict, delivered: list[dict]) -> dict:
    return {
        "ok": bool(delivered) and all(item["ok"] for item in delivered),
        "issues": len(issues),
        "prs": len(pulls),
        "journal": journal,
        "channels": delivered,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-file", required=True, help="JSON конфига автоматизации")
    parser.add_argument("--since-ts", type=int, default=0, help="начало периода, epoch ms")
    parser.add_argument("--until-ts", type=int, default=0, help="конец периода, epoch ms")
    parser.add_argument("--result-file", required=True, help="куда записать result-JSON")
    args = parser.parse_args(argv)

    with open(args.config_file, encoding="utf-8") as file:
        config = json.load(file)
    channels = (config.get("report") or {}).get("channels") or []
    if not channels:
        # Дайджест без каналов — собранный текст в никуда; форма конфига это
        # отсекает (parseAutomationConfig), здесь — второй рубеж на всякий случай.
        print("::error::в конфиге нет каналов report.channels — доставлять некуда", file=sys.stderr)
        return 1

    until_ts = args.until_ts or int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    since_ts = args.since_ts or until_ts - 7 * 24 * 3600 * 1000
    date_from = dt.datetime.fromtimestamp(since_ts / 1000, dt.timezone.utc).date()
    date_to = dt.datetime.fromtimestamp(until_ts / 1000, dt.timezone.utc).date()

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        print("::error::GITHUB_REPOSITORY не задан", file=sys.stderr)
        return 1

    issues, pulls = collect_issues_and_prs(repo, date_from, date_to)
    journal = collect_journal(os.environ.get("HANDS_URL", ""),
                              os.environ.get("HANDS_TOKEN", ""), since_ts, until_ts)
    text = format_digest(repo, date_from, date_to, issues, pulls, journal)
    delivered = deliver_all(channels, text)
    result = build_result(issues, pulls, journal, delivered)

    with open(args.result_file, "w", encoding="utf-8") as file:
        json.dump(result, file, ensure_ascii=False, indent=1)

    for item in delivered:
        mark = "OK " if item["ok"] else "FAIL"
        print(f"{mark} {item['type']} {item['target']}: {item['detail']}")
    if not result["ok"]:
        failed = ", ".join(item["type"] for item in delivered if not item["ok"])
        print(f"::error::каналы не доставили: {failed} — остальные доставлены, отказ виден выше", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
