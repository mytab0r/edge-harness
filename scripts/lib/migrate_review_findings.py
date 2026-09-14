#!/usr/bin/env python3
# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---
"""Миграция 110 задач-хвостов «Хвост чеклиста ревью PR #N» в реестр находок
(#1262, этап 2/3 — openspec/changes/review-findings-registry/proposal.md,
design.md развилка 5).

Закрывать задачи ДО того, как реестр наполнен их содержимым, нельзя (обратный
порядок уничтожает 412 живых находок) — эта команда только СОБИРАЕТ план
(`plan_migration`, чистая функция без сети) и печатает отчёт; запись в реестр
и закрытие issues — только с явным `--apply` (см. cmd_migrate), отдельным,
осознанным запуском, не побочным эффектом чтения.

Стратегия определения файла находки (design.md, развилка 5):
1. Точный путь, упомянутый в самом тексте находки (`guess_exact_file`) — токен
   вида `путь/файл.py` или `файл.py:123`, реально существующий в дереве
   репозитория на момент миграции (полным путём или однозначным хвостом).
2. Не найден — broadcast на ВСЕ файлы, изменённые породившим PR (находка
   гарантированно всплывёт при следующем ревью любого из них, ценой более
   широкого показа).
3. PR недоступен (удалён/приватный форк/404) — ручной разбор (`method:
   manual`, file=None) — не теряется (запись остаётся в плане с
   `tail_issue`/`title` для человека), но не автоматизируется дальше.

Запуск (только чтение, безопасно вне CI):
    python scripts/lib/migrate_review_findings.py plan --repo mytab0r/edge-harness

Запись (только внутри CI/с правами contents:write на data/review-findings):
    python scripts/lib/migrate_review_findings.py apply --repo mytab0r/edge-harness
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
from datetime import datetime, timezone

_LIB = Path(__file__).resolve().parent
_rf_spec = importlib.util.spec_from_file_location("review_findings", _LIB / "review_findings.py")
review_findings = importlib.util.module_from_spec(_rf_spec)
_rf_spec.loader.exec_module(review_findings)


def gh(*args: str):
    result = subprocess.run(
        ["gh", "api", *args],
        capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "NO_COLOR": "1"},
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh api {' '.join(args[:2])}: {result.stderr.strip()}")
    return json.loads(result.stdout) if result.stdout.strip() else None


# ── Чистая логика (тестируется без сети, test_migrate_review_findings.py) ────

TAIL_TITLE_RE = re.compile(r"^Хвост чеклиста ревью PR #(\d+)$")
# Формат находки — review_checklist.tail_issue_body (до удаления функции
# этой же правкой): "- [ ] <текст>" одной строкой на находку.
FINDING_LINE_RE = re.compile(r"^- \[ \] (.+)$")
# Путь файла внутри свободного текста находки: токен без пробелов,
# заканчивающийся известным расширением репозитория, опциональный
# ":номер_строки" хвост (реальный пример из ревизии: "plugin-forge.yml:894").
FILE_TOKEN_RE = re.compile(
    r"(?<![\w/])([\w][\w./-]*\.(?:py|ts|tsx|js|jsx|md|yml|yaml|sh|json|toml|cfg|ini))"
    r"(?::\d+)?(?![\w])"
)


def extract_findings(issue_body: str) -> list[str]:
    """Строки находок из тела хвоста — одна на пункт `- [ ] текст`."""
    lines = []
    for line in (issue_body or "").splitlines():
        match = FINDING_LINE_RE.match(line.strip())
        if match:
            lines.append(match.group(1).strip())
    return lines


def pr_number_from_title(title: str) -> int | None:
    match = TAIL_TITLE_RE.match((title or "").strip())
    return int(match.group(1)) if match else None


def guess_exact_file(finding_text: str, repo_files: set[str]) -> str | None:
    """Первый упомянутый в тексте путь, реально существующий в дереве
    репозитория на момент миграции — полным путём или ОДНОЗНАЧНЫМ хвостом
    (без ведущих директорий, живой случай ревизии: текст называл
    "dependabot_alert_watch.py" без префикса scripts/orchestra/)."""
    for match in FILE_TOKEN_RE.finditer(finding_text):
        candidate = match.group(1)
        if candidate in repo_files:
            return candidate
        tail_matches = [f for f in repo_files if f == candidate or f.endswith("/" + candidate)]
        if len(tail_matches) == 1:
            return tail_matches[0]
    return None


# Потолок broadcast-фанаута (замер живого прогона 2026-09-14: 194 находки
# без точного файла дали 1611 записей broadcast, медианный фанаут 15 файлов
# на находку, максимум 100 — PR #409). Broadcast на ЭТО число файлов не
# «выписка по файлу», а шум: находка появлялась бы в промпте ЛЮБОГО PR,
# тронувшего любой из сотни файлов, и закрытие по одному id не снимает
# остальные копии. Свыше потолка — не broadcast НИКУДА (частичный broadcast
# произвольного подмножества файлов не более принципиален, чем полный) —
# честный ручной разбор (`manual`), не молчаливое раздувание реестра.
MAX_BROADCAST_FANOUT = 20


def plan_migration(tails: list[dict], repo_files: set[str], pr_files_lookup) -> tuple[list[dict], dict]:
    """tails — [{"number", "title", "body"}, ...] (прод-форма issues API,
    is:closed уже отфильтровано вызывающим). pr_files_lookup(pr_number) ->
    list[str] | None (None — PR недоступен, пуст список — PR без файлов,
    оба трактуются одинаково: broadcast невозможен, ручной разбор). PR с
    более чем MAX_BROADCAST_FANOUT файлами — тоже ручной разбор (см.
    константу выше).

    Возвращает (план записей реестра, статистика по методам)."""
    plan: list[dict] = []
    stats = {"exact": 0, "broadcast": 0, "manual": 0, "total_findings": 0, "tails": len(tails)}
    for tail in tails:
        pr_number = pr_number_from_title(tail["title"])
        findings = extract_findings(tail.get("body") or "")
        stats["total_findings"] += len(findings)
        for text in findings:
            exact = guess_exact_file(text, repo_files)
            if exact:
                plan.append({"tail_issue": tail["number"], "source_pr": pr_number,
                             "file": exact, "title": text, "detail": "", "method": "exact"})
                stats["exact"] += 1
                continue
            pr_files = pr_files_lookup(pr_number) if pr_number else None
            if pr_files and len(pr_files) <= MAX_BROADCAST_FANOUT:
                for file in pr_files:
                    plan.append({"tail_issue": tail["number"], "source_pr": pr_number,
                                 "file": file, "title": text, "detail": "", "method": "broadcast"})
                stats["broadcast"] += 1
                continue
            plan.append({"tail_issue": tail["number"], "source_pr": pr_number,
                         "file": None, "title": text, "detail": "", "method": "manual"})
            stats["manual"] += 1
    return plan, stats


# ── Сетевые обёртки (не тестируются юнит-тестами — интеграционная часть) ─────

def fetch_open_tails(repo: str) -> list[dict]:
    """Все открытые issues с заголовком «Хвост чеклиста ревью PR #N» —
    постранично (класс #308, тот же приём, что review_labels.list_pages)."""
    tails: list[dict] = []
    page = 1
    while True:
        query = f'repo:{repo} "Хвост чеклиста ревью PR" in:title is:open is:issue'
        payload = gh(f"search/issues?q={urllib.parse.quote(query)}&per_page=100&page={page}")
        items = payload.get("items", []) if payload else []
        if not items:
            break
        tails.extend({"number": item["number"], "title": item["title"], "body": item.get("body") or ""}
                     for item in items)
        if len(items) < 100:
            break
        page += 1
    return tails


def fetch_repo_files(repo: str, ref: str = "main") -> set[str]:
    payload = gh(f"repos/{repo}/git/trees/{ref}?recursive=1")
    return {entry["path"] for entry in (payload or {}).get("tree", []) if entry.get("type") == "blob"}


def fetch_pr_files(repo: str, pr_number: int) -> list[str] | None:
    try:
        page = 1
        files: list[str] = []
        while True:
            payload = gh(f"repos/{repo}/pulls/{pr_number}/files?per_page=100&page={page}")
            if not payload:
                break
            for f in payload:
                # status == "removed" — файл УДАЛЁН этим PR: такой путь не
                # появится в будущем диффе, пока его специально не воссоздадут,
                # значит broadcast на него — та же заморозка находки навсегда,
                # от которой design.md (развилка 5) отверг вариант
                # `_legacy/pr-<N>` («такой путь никогда не встретится в диффе
                # — находка фактически ЗАМОРАЖИВАЕТСЯ навсегда»). files API
                # отдаёт удалённым файлам status: "removed" — без фильтра
                # план считал бы такую находку «перенесённой broadcast», а
                # статистика «ноль потерянных» врала бы (находка ревью
                # PR #1268). Честный исход — файл не попадает в выписку
                # broadcast, находка без точного пути уходит в manual.
                if f.get("status") == "removed":
                    continue
                files.append(f["filename"])
            if len(payload) < 100:
                break
            page += 1
        return files or None
    except RuntimeError as error:
        if "HTTP 404" in str(error):
            return None
        raise


def cmd_plan(args: argparse.Namespace) -> int:
    tails = fetch_open_tails(args.repo)
    repo_files = fetch_repo_files(args.repo)
    cache: dict[int, list[str] | None] = {}

    def lookup(pr_number: int):
        if pr_number not in cache:
            cache[pr_number] = fetch_pr_files(args.repo, pr_number)
        return cache[pr_number]

    plan, stats = plan_migration(tails, repo_files, lookup)
    print(f"Открытых хвостов: {stats['tails']}")
    print(f"Находок суммарно: {stats['total_findings']}")
    print(f"  точный файл (exact): {stats['exact']}")
    print(f"  broadcast на файлы PR: {stats['broadcast']}")
    print(f"  ручной разбор (PR недоступен): {stats['manual']}")
    out = Path(args.out) if args.out else None
    if out:
        out.write_text(json.dumps({"plan": plan, "stats": stats}, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        print(f"План записан: {out}")
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    """Пишет план в реестр ОДНИМ прогоном (не по находке) — требует
    contents: write (см. proposal.md — тот же контур прав, что
    scheduler.py::after_merge). Не запускать вне CI/без явного мандата
    владельца на прод-запись (AGENTS.md, git-identity).

    Идемпотентен: перед add_finding пропускается запись, у которой тройка
    (file, title, source_pr) уже есть в реестре, — повторный прогон (ретрай
    после сбоя посреди записи, «а записалось ли?») не задваивает находки.
    Раньше идемпотентность по заголовку имел СТАРЫЙ носитель (сама задача-хвост
    не могла существовать дважды), реестр без этого фильтра накапливал бы по
    копии за каждый прогон (находка ревью PR #1268, чеклист тела)."""
    plan_path = Path(args.plan)
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    plan = payload["plan"]
    now = datetime.now(timezone.utc).isoformat()
    registry, sha = review_findings.fetch_registry(gh, args.repo)
    existing = {(f["file"], f["title"], f.get("source_pr"))
                for f in registry["findings"]}
    added = skipped = 0
    for item in plan:
        if not item.get("file"):
            continue
        source_pr = item.get("source_pr") or 0
        key = (item["file"], item["title"], source_pr)
        if key in existing:
            skipped += 1
            continue
        review_findings.add_finding(registry, item["file"], item["title"],
                                    item.get("detail", ""), source_pr, now)
        existing.add(key)  # и от дублей ВНУТРИ самого плана
        added += 1
    if added == 0:
        print(f"Новых находок нет: {skipped} уже в реестре "
              "(повторный прогон безопасен, запись не нужна).")
        return 0
    review_findings.write_registry(
        gh, args.repo, registry, sha,
        f"review-findings: миграция {added} находок из задач-хвостов (#1262)")
    print(f"Записано {added} новых находок, пропущено {skipped} уже существующих "
          f"({review_findings.REGISTRY_BRANCH}).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="только чтение — собрать и напечатать план миграции")
    plan.add_argument("--repo", required=True)
    plan.add_argument("--out", help="путь для сохранения плана JSON (вход apply)")
    plan.set_defaults(func=cmd_plan)

    apply_cmd = sub.add_parser("apply", help="записать план в реестр (contents: write)")
    apply_cmd.add_argument("--repo", required=True)
    apply_cmd.add_argument("--plan", required=True, help="путь к JSON, сохранённому командой plan --out")
    apply_cmd.set_defaults(func=cmd_apply)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
