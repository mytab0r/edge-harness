#!/usr/bin/env python3
"""Ранний отказ по квоте GitHub API вместо 403 посреди дорогого job'а (#454).

Повод (живые случаи 2026-09-06): PR #428 — оба обязательных гейта (`test`,
`contract`) упали с `API rate limit exceeded for installation` посреди
чужого скрипта; PR #453 — прогон `ai-review` упал на
`gh api repos/.../pulls/453`. `GITHUB_TOKEN` даёт 1000 запросов/час НА
РЕПОЗИТОРИЙ, общих на ВСЕ workflow одновременно
(docs/research/21-github-actions.md, «API-лимиты: настоящий потолок»).
`orchestra` тратит 150-250 REST-вызовов за один прогон при непустой очереди
(issue #443, замер по коду) — это и есть основание порога: 300 (250 + ~20%
запас) отсекает прогон РАНЬШЕ, чем он рискнёт упасть 403 посреди своей же
работы, и оставляет не меньше 700 запросов/час другим потребителям того же
часа (contract на каждый открытый PR, ai-review, повторные
workflow_dispatch) — все они делят ОДИН и тот же бюджет, порог общий и
консервативный, откалиброванный по самому дорогому потребителю.

Использование (шаг workflow, ДО дорогого пути; последующие шаги гасятся
условием `if:` на его output):

    - id: quota
      env:
        GH_TOKEN: ${{ github.token }}
      run: python scripts/lib/rate_guard.py --job orchestra
    - if: steps.quota.outputs.skip != 'true'
      run: ...дорогой путь...

Граница (AGENTS.md, «fail loud, не silent-wrong»): отказ по квоте
(`skip=true`, код возврата 0, аннотация `::warning::`) и отказ по ЛЮБОЙ
ДРУГОЙ причине (сеть, права токена, битый ответ — код возврата 1, аннотация
`::error::`) обязаны различаться и кодом, и префиксом — чинятся они
по-разному, «квоты нет» не должно выглядеть как «сеть легла» и наоборот.

Третий случай, который легко спутать со вторым и который стоил блокирующей
находки ai-review PR #1451: зонд применяемого счётчика сам получил 403
«rate limit exceeded». Это НЕ «не смог измерить» — счётчик ответил, и ответ
точный: пусто. Поэтому исход первый (`skip=true`, код 0, `::warning::`), а
не второй. Первая редакция роняла здесь красным, то есть в самом тяжёлом
состоянии объекта воспроизводила ровно дефект #1437, ещё и детерминированно:
первый шаг каждого PR красный, пока бак не сбросится.

ДВА ИСТОЧНИКА ОСТАТКА, И ОНИ НЕ СОГЛАСНЫ (#1437, замер 2026-09-22).
`gh api rate_limit` не тратит собственную квоту — и ровно поэтому НЕ
отражает счётчик, который применяется: эндпоинт исключён из лимитирования и
отдаёт свой, отдельный бак. Живой случай, прогон 35715554412, job `test`,
PR #1449:

    10:24:21  rate_guard: квота ок (5000/5000) для job'а «repo-ci-invariants»
    10:27:02  ##[error]repo_invariants: gh api …/issues/120/comments:
              gh: API rate limit exceeded for installation … (HTTP 403)

Полный бак и 403 по тому же токену, в том же job'е, через 2 мин 41 с. Это
не «за две минуты выбрали 5000» — `remaining` РАВЕН `limit`, то есть на том
счётчике не потрачено НИЧЕГО, пока применяемый уже пуст. Значит `resources.
core` из `rate_limit` — не тот бак, и предсказывать 403 им нельзя в
принципе, а не «порог подобран не так».

Второй источник — заголовки `X-Ratelimit-*` ЛЮБОГО обычного ответа: их
отдаёт тот самый счётчик, который и приводит к 403 (`X-Ratelimit-Resource`
называет бак прямо). Поэтому `fetch_enforced` делает один дешёвый реальный
запрос и читает остаток из заголовков.

Решение принимается по МЕНЬШЕМУ из двух остатков. Это осознанно
консервативно: что заголовки для `github.token` показывают именно
применяемый бак — из логов CI ещё НЕ подтверждено (см. `docs/research/
21-github-actions.md`, раздел про #1437), подтверждено обратное про
`rate_limit`. Минимум безопасен при любом исходе замера, а оба числа
печатаются рядом — следующий же прогон с 403 даст сравнение прямо в логе,
без отдельной кампании.

Цена, названная вслух: один настоящий запрос на вызов гейта (было ноль).
Против 150-250 запросов дорогого пути, который он гасит, это дёшево; но
«бесплатно» — больше неверное слово.

Запуск: python scripts/lib/rate_guard.py --job <имя>
Тесты:  python -m pytest scripts/lib/test_rate_guard.py -q
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

# Один порог на все три точки вызова (orchestra, ai-review::review,
# repo-ci::test-инварианты, #454) — они делят один и тот же бюджет
# GITHUB_TOKEN (1000/час/репозиторий), поэтому один консервативный порог,
# откалиброванный по самому дорогому потребителю (orchestra, 150-250
# запросов/прогон), безопасен и для более дешёвых потребителей: они выйдут
# раньше, чем реально исчерпают себя, а не позже.
DEFAULT_THRESHOLD = 300


# Дословный текст отказа GitHub, снятый с живого лога (прогон 35715554412):
# «API rate limit exceeded for installation». Вторичный лимит формулируется
# иначе («exceeded a secondary rate limit»), но несёт то же значение для
# решения — оба означают «счётчик ответил: пусто», а не «измерить нечем».
RATE_LIMIT_REFUSAL_RE = re.compile(r"rate limit exceeded|secondary rate limit", re.IGNORECASE)


class QuotaCheckFailed(RuntimeError):
    """Настоящий сбой проверки (не «квоты мало», а «не смог узнать») — сеть,
    права токена, битый ответ. Отдельный класс исключения — вызывающий код
    не может перепутать эту причину с обычным skip (см. докстринг модуля,
    «Граница»)."""


class QuotaExhausted(QuotaCheckFailed):
    """Зонд применяемого счётчика получил 403 «rate limit exceeded» — это
    ИЗМЕРЕНИЕ, а не сбой измерения (находка ai-review PR #1451, блокирующая).

    Разница решает исход job'а. Первая редакция этого PR роняла гейт красным
    на любом отказе зонда, и в самом тяжёлом состоянии объекта — бак пуст —
    получалось ровно то, что #1437 и называет дефектом: обязательная проверка
    красная по причине, к диффу автора отношения не имеющей, теперь ещё и
    детерминированно, на ПЕРВОМ шаге каждого PR до сброса бака.

    Счётчик, ответивший «пусто», ответил точно. Значит это обычный skip:
    `skip=true`, код 0, `::warning::` — ровно та семантика, которую «Граница»
    в докстринге модуля обещает для «квоты нет». Красный остаётся для сети,
    прав токена и битого ответа, где остаток и правда неизвестен.

    Наследуется от QuotaCheckFailed намеренно: вызывающий, который ловит
    только базовый класс, продолжит работать как раньше — ошибка не
    протечёт наружу необработанной, если кто-то забудет про подкласс."""


def fetch_core(env: dict | None = None) -> dict:
    """`.resources.core` живого `gh api rate_limit` — единственный сетевой
    вызов этого скрипта. Не тратит собственную квоту (см. докстринг)."""
    result = subprocess.run(
        ["gh", "api", "rate_limit"],
        capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, **(env or {}), "NO_COLOR": "1"},
    )
    if result.returncode != 0:
        raise QuotaCheckFailed(
            result.stderr.strip() or f"gh api rate_limit завершился с кодом {result.returncode}")
    try:
        body = json.loads(result.stdout)
        return body["resources"]["core"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise QuotaCheckFailed(f"ответ rate_limit не разобран: {error}")


_HEADER_RE = re.compile(r"^x-ratelimit-([a-z]+):\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


def parse_ratelimit_headers(raw: str) -> dict:
    """`X-Ratelimit-*` из сырого ответа `gh api -i` → тот же словарь, что
    отдаёт `fetch_core` (limit/used/remaining/reset + resource).

    Заголовки — прод-форма: регистр у GitHub «X-Ratelimit-Remaining», но
    HTTP-заголовки регистронезависимы, и прокси их нормализуют по-разному —
    разбор регистронезависимый намеренно, иначе гейт ослепнет от смены
    регистра и скажет «не смог прочитать» там, где данные есть."""
    found = {name.lower(): value for name, value in _HEADER_RE.findall(raw)}
    missing = [name for name in ("limit", "remaining", "reset") if name not in found]
    if missing:
        raise QuotaCheckFailed(
            f"в ответе нет заголовков X-Ratelimit-{'/'.join(missing)} — "
            f"остаток по применяемому счётчику прочитать нечем")
    try:
        parsed = {name: int(found[name]) for name in ("limit", "remaining", "reset")}
    except ValueError as error:
        raise QuotaCheckFailed(f"заголовок X-Ratelimit-* не число: {error}")
    parsed["used"] = int(found["used"]) if found.get("used", "").isdigit() else (
        parsed["limit"] - parsed["remaining"])
    parsed["resource"] = found.get("resource", "?")
    return parsed


def fetch_enforced(repo: str, env: dict | None = None) -> dict:
    """Остаток по ПРИМЕНЯЕМОМУ счётчику — из заголовков одного настоящего
    запроса (`gh api -i repos/<repo>`), а не из `rate_limit` (тот отдаёт
    свой бак, см. докстринг модуля, #1437).

    Эндпоинт выбран самый дешёвый из тех, что доступны `github.token` в
    своём репозитории, и он же тратит ровно один запрос — цена названа в
    докстринге модуля."""
    result = subprocess.run(
        ["gh", "api", "-i", f"repos/{repo}"],
        capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, **(env or {}), "NO_COLOR": "1"},
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or f"gh api -i repos/{repo} завершился с кодом {result.returncode}"
        if RATE_LIMIT_REFUSAL_RE.search(detail):
            raise QuotaExhausted(detail)
        raise QuotaCheckFailed(detail)
    return parse_ratelimit_headers(result.stdout)


def should_skip(core: dict, threshold: int) -> bool:
    """True — квоты меньше порога, дорогой путь пропускаем. Строго `<`:
    остаток РОВНО на пороге ещё считается достаточным («порог» — это «мало»,
    не «впритык к порогу тоже мало»)."""
    return core["remaining"] < threshold


def reset_human(core: dict) -> str:
    return datetime.fromtimestamp(core["reset"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def write_output(pairs: dict[str, str]) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as file:
        for key, value in pairs.items():
            file.write(f"{key}={value}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", required=True, help="имя job'а/шага — только для сообщения")
    parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    parser.add_argument(
        "--repo", default=os.environ.get("GITHUB_REPOSITORY", ""),
        help="owner/repo, у которого спрашивают применяемый счётчик; "
             "по умолчанию $GITHUB_REPOSITORY (в Actions задан всегда)")
    args = parser.parse_args()

    repo = (args.repo or "").strip()
    if not repo:
        # Без репозитория применяемый счётчик прочитать нечем, а читать
        # ТОЛЬКО rate_limit — это и есть дефект #1437 («полный бак» за две
        # минуты до 403). Молчать и пропускать дорогой путь вперёд нельзя:
        # «возможности измерить нет» и «квота есть» — разные факты.
        print("::error::rate_guard: репозиторий не назван (--repo или $GITHUB_REPOSITORY) — "
              f"остаток по применяемому счётчику для job'а «{args.job}» прочитать нечем (#1437)")
        return 1

    try:
        core = fetch_core()
    except QuotaCheckFailed as error:
        # Настоящий сбой — не квота: другой код возврата, другой префикс
        # (см. докстринг модуля, «Граница»). Дорогой путь после этого не
        # должен молча продолжиться — вызывающий workflow обязан покраснеть
        # именно здесь, а не тонуть в первом же 403 внутри самого пути.
        print(f"::error::rate_guard: не смог прочитать квоту GitHub API для job'а «{args.job}»: {error}")
        return 1

    try:
        enforced = fetch_enforced(repo)
    except QuotaExhausted as error:
        # Счётчик ответил «пусто» — это измерение, а не сбой измерения
        # (находка ai-review PR #1451). Ронять здесь обязательную проверку
        # значило бы воспроизводить дефект #1437 в самом тяжёлом состоянии
        # объекта, причём детерминированно.
        write_output({
            "skip": "true",
            "reason": "применяемый счётчик GitHub API исчерпан (403 от зонда)",
            "reset": "не отдан в отказе",
        })
        print(
            f"::warning::rate_guard: применяемый счётчик GitHub API исчерпан для job'а "
            f"«{args.job}» — дорогой путь пропущен, job остаётся зелёным. Ответ GitHub: {error}"
        )
        return 0
    except QuotaCheckFailed as error:
        print("::error::rate_guard: не смог прочитать остаток по ПРИМЕНЯЕМОМУ счётчику "
              f"(заголовки X-Ratelimit-*) для job'а «{args.job}»: {error}")
        return 1

    # Оба числа печатаются рядом ВСЕГДА, а не только при пропуске: это и есть
    # замер, которого требует #1437 — следующий же прогон, упавший 403, даст в
    # том же логе оба остатка на момент проверки, без отдельной кампании.
    print(
        f"rate_guard: rate_limit говорит {core['remaining']}/{core['limit']}, "
        f"заголовки ({enforced['resource']}) — {enforced['remaining']}/{enforced['limit']} "
        f"(use {enforced['used']}); решение по меньшему (#1437)"
    )

    # Меньшее из двух. Источники расходятся (доказано: полный бак rate_limit за
    # 2 мин 41 с до 403), какой из них верен для github.token — замеряется этим
    # же выводом; минимум безопасен при любом исходе замера.
    applied = min((core, enforced), key=lambda source: source["remaining"])
    remaining, limit = applied["remaining"], applied["limit"]
    if should_skip(applied, args.threshold):
        reset_at = reset_human(applied)
        write_output({
            "skip": "true",
            "reason": f"квота {remaining}/{limit} ниже порога {args.threshold}",
            "reset": reset_at,
        })
        print(
            f"::warning::rate_guard: квота GitHub API почти исчерпана "
            f"({remaining}/{limit}, порог {args.threshold}) для job'а «{args.job}» — "
            f"дорогой путь пропущен, сброс в {reset_at}"
        )
        return 0

    write_output({"skip": "false"})
    print(f"rate_guard: квота ок ({remaining}/{limit}) для job'а «{args.job}»")
    return 0


if __name__ == "__main__":
    sys.exit(main())
