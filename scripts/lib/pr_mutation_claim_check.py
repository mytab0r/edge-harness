#!/usr/bin/env python3
"""Живая проверка заявлений автора PR (#968) — glue-скрипт вокруг чистой
логики `mutation_claim.py`. Смотри докстринг того модуля для контракта
блоков «## Доказательство мутацией» / «## Класс закрыт» / «## Непроверено
машиной».

Три независимых проверки одного PR, каждая печатает, что именно проверила и
с каким исходом (fail loud, не молчаливый пропуск):

  1. **Непроверяемые заявления без пометки** — ВСЕГДА, дёшево (regex по телу
     PR, без сети и без выполнения кода). НЕ блокирует мерж (`::warning::` —
     см. обоснование в докстринге ниже, `_report_unverified`): формулировки
     класса «прогнал живьём» встречаются и в честном описании находок ревью
     о ЧУЖИХ инцидентах (напр. пересказ #893/#905 в тексте PR), не только
     как заявление о своей работе — жёсткая блокировка по одной фразе давала
     бы много ложных отказов. Пометка делает факт видимым читателю; решение
     остаётся за ревью.
  2. **«Класс закрыт»** — ВСЕГДА, дёшево (один `git grep` на заявленный
     паттерн). Блокирует: заявленное число совпадений — то же самое, что
     утверждение «мутация доказана» для мутации, разница только в форме
     проверки.
  3. **«Мутация доказана»** — ТОЛЬКО для PR, меняющих `scripts/ci/guards/
     *.sh` (см. `mutation_claim.guard_catalog_paths_changed`). Дорогая
     (применяет патч, дважды гоняет pytest, откатывает) — ограничена этим
     подмножеством: за 15 дней истории репозитория (2026-08-28…2026-09-11,
     328 коммитов) ровно 12 коммитов трогали `scripts/ci/guards/*.sh`
     (~3,7%, около одного PR в один-два дня) — не каждый PR платит цену
     мутационного прогона. Блокирует: обязательна для таких PR (её
     отсутствие — контрактное нарушение, не «не заявлено»), и обязана
     закончиться вердиктом `proved`.

Вход: событие `pull_request` (тело PR, номер — из $GITHUB_EVENT_PATH, без
`gh api`); список изменённых файлов — `gh api .../pulls/{n}/files`
(GH_TOKEN уже в env на уровне шага-перебора каталога гвардий, #749, см.
repo-ci.yml). На событиях, отличных от pull_request (push, workflow_dispatch)
— проверка пропускается целиком с явной причиной (тела PR нет), это не сбой.

Запуск: python scripts/lib/pr_mutation_claim_check.py
"""
from __future__ import annotations

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import json
import os
import subprocess
import sys

_MC_SPEC = importlib.util.spec_from_file_location(
    "mutation_claim", Path(__file__).resolve().parent / "mutation_claim.py")
mutation_claim = importlib.util.module_from_spec(_MC_SPEC)
sys.modules["mutation_claim"] = mutation_claim  # @dataclass + отложенные аннотации, см. mutation_claim.py
_MC_SPEC.loader.exec_module(mutation_claim)  # type: ignore[union-attr]

REPO_ROOT = mutation_claim.REPO_ROOT


class GhError(RuntimeError):
    pass


def _gh_api(*args: str) -> dict | list | None:
    """Минимальный локальный вызов `gh api` (та же форма, что pulse_guard.gh/
    claim_task.gh — этот модуль намеренно не импортирует ни один из них:
    оба лежат в scripts/orchestra/, а логика здесь принадлежит слою
    scripts/lib/, обратная зависимость lib → orchestra не заводится ради
    одного read-only вызова)."""
    result = subprocess.run(
        ["gh", "api", *args],
        capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "NO_COLOR": "1"},
    )
    if result.returncode != 0:
        raise GhError(result.stderr.strip())
    return json.loads(result.stdout) if result.stdout.strip() else None


def list_pr_changed_files(repo: str, number: int) -> list[str]:
    """Пути файлов PR, статус которых не `removed` (постранично, класс #308 —
    не читать только первую страницу)."""
    paths: list[str] = []
    page = 1
    while True:
        chunk = _gh_api(f"repos/{repo}/pulls/{number}/files?per_page=100&page={page}") or []
        for entry in chunk:
            if entry.get("status") != "removed":
                paths.append(entry["filename"])
        if len(chunk) < 100:
            break
        page += 1
    return paths


def _report_unverified(body: str) -> bool:
    """Возвращает True, если найдены НЕ раскрытые непроверяемые заявления —
    только для отчёта, не влияет на exit code (см. докстринг модуля)."""
    check = mutation_claim.check_unverified_disclosure(body)
    if not check.mentions:
        print("mutation-claim: непроверяемых формулировок в теле PR не найдено")
        return False
    if check.disclosed:
        print(
            f"mutation-claim: непроверяемые формулировки найдены и раскрыты "
            f"под «{mutation_claim.UNVERIFIED_HEADING}»: {check.mentions}"
        )
        return False
    print(
        f"::warning::mutation-claim: тело PR содержит формулировки, которые "
        f"машина проверить не может ({check.mentions}), но не помечает их "
        f"под «{mutation_claim.UNVERIFIED_HEADING}» — читатель может принять "
        f"прозу за доказательство. Добавь секцию с этим заголовком, "
        f"перечислив, что именно непроверено."
    )
    return True


def _run_class_closed(body: str, repo_root: Path) -> bool:
    """True — есть провал (заявленное число не совпало с фактом)."""
    try:
        claims = mutation_claim.parse_class_closed_claims(body)
    except mutation_claim.MutationClaimFormatError as error:
        print(f"::error::{error}")
        return True
    failed = False
    for claim in claims:
        ok, matches = mutation_claim.run_class_closed_check(repo_root, claim)
        if ok:
            print(
                f"mutation-claim[класс закрыт]: подтверждено — паттерн "
                f"«{claim.pattern}», совпадений {len(matches)} (заявлено {claim.expected_count})"
            )
        else:
            failed = True
            print(
                f"::error::mutation-claim[класс закрыт]: заявлено "
                f"{claim.expected_count} совпадений паттерна «{claim.pattern}», "
                f"фактически {len(matches)}: {matches}"
            )
    return failed


def _run_mutation_proof(body: str, changed_files: list[str], repo_root: Path) -> bool:
    """True — есть провал (гвардия обязательна и не доказана / отсутствует /
    неверной формы). Печатает, требуется ли проверка вовсе — молчаливого
    пропуска (когда PR реально меняет гвардию) быть не должно."""
    guard_paths = mutation_claim.guard_catalog_paths_changed(changed_files)
    if not guard_paths:
        print(
            "mutation-claim: PR не меняет scripts/ci/guards/*.sh — "
            "мутационная проверка не требуется (ограничение цены, #968)"
        )
        return False

    print(f"mutation-claim: PR меняет гвардию(и) каталога: {guard_paths} — требуется «{mutation_claim.MUTATION_HEADING}»")
    try:
        claims = mutation_claim.parse_mutation_claims(body)
    except mutation_claim.MutationClaimFormatError as error:
        print(f"::error::{error}")
        return True
    if not claims:
        print(
            f"::error::mutation-claim: PR меняет {guard_paths}, но тело PR не "
            f"несёт блока «{mutation_claim.MUTATION_HEADING}» — заявление о "
            f"мутации обязательно для PR, добавляющего/меняющего гвардию каталога "
            f"scripts/ci/guards/*.sh (issue #968)"
        )
        return True

    failed = False
    for claim in claims:
        outcome = mutation_claim.run_mutation_proof(repo_root, claim)
        print(outcome.report())
        if outcome.verdict != "proved":
            failed = True
    return failed


def main() -> int:
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if event_name != "pull_request" or not event_path:
        print(
            f"mutation-claim: событие «{event_name or '?'}» не pull_request — "
            "тела PR нет, живая проверка заявлений пропущена (не сбой)"
        )
        return 0

    event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    pr = event.get("pull_request") or {}
    number = pr.get("number")
    body = pr.get("body") or ""
    repo = os.environ["GITHUB_REPOSITORY"]

    if not number:
        print("::error::mutation-claim: событие pull_request без pull_request.number — неожиданная форма события")
        return 1

    _report_unverified(body)  # только предупреждение, exit code не меняет (см. докстринг)

    failed = _run_class_closed(body, REPO_ROOT)

    try:
        changed_files = list_pr_changed_files(repo, number)
    except GhError as error:
        print(f"::error::mutation-claim: список файлов PR #{number} не прочитан ({error})")
        return 1

    failed = _run_mutation_proof(body, changed_files, REPO_ROOT) or failed

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
