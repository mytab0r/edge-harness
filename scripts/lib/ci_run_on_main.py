"""Один критерий: прогон workflow относится к состоянию, которое реально
живёт в main, а не к ветке-кандидату (PR/`workflow_dispatch` на чужой ветке)
(issue #925).

## Класс

`conclusion=success` прогона НИЧЕГО не говорит о состоянии main, если
`head_sha` этого прогона не является предком main (или самим main) —
доказано живым случаем: три подряд «зелёных» прогона `deploy-worker.yml`
(2026-09-12T00:06–00:17Z) были `workflow_dispatch` на ветке
`agent/678-rows-written-namespace`, а последний прогон, реально
триггернутый push'ем в main (2026-09-11T14:24Z, слияние PR #943), — красный
(`worker-configuration.d.ts устарел`). Любое место, которое читает «есть ли
зелёный прогон этого workflow» как доказательство состояния main — без
проверки предка — рискует закрыть живой отказ прода вслепую (issue #925,
задача о ci-failure).

## Критерий

GitHub Compare API `repos/{repo}/compare/{main}...{head_sha}` возвращает
`status` ОТНОСИТЕЛЬНО base (`main`):
  - `identical` — `head_sha` == main (тот же коммит).
  - `behind`    — `head_sha` позади main, то есть является его ПРЕДКОМ:
                  всё содержимое `head_sha` уже поглощено main. Это тот
                  случай, когда прогон был на main, а потом main ушёл
                  дальше (обычный порядок: пушим, тикает workflow, main
                  через 15 минут снова уезжает вперёд следующим мержем).
  - `ahead`     — `head_sha` несёт коммиты, которых НЕТ в main (main —
                  предок `head_sha`, обратное направление). PR-ветка,
                  ушедшая от main вперёд, не сливаясь, — типичный случай.
  - `diverged`  — ни один не предок другого (обычная PR-ветка после того,
                  как main успел уйти вперёд своим путём, — живой случай
                  #925: `ahead_by=3/4, behind_by=21`).

Только `identical`/`behind` означают «состояние `head_sha` целиком внутри
main прямо сейчас» — единственные два случая, где исход прогона можно
читать как факт о main. `ahead`/`diverged` — прогон ветки-кандидата, его
исход ничего не доказывает о main, каким бы зелёным он ни был.

Одно место правды: любой код, решающий «эта задача/чек закрыты, потому что
следующий прогон workflow зелёный», обязан звать `run_is_on_main`/
`latest_run_on_main` отсюда, а не проверять `conclusion` в одиночку.

Запуск тестов: python -m pytest scripts/lib/test_ci_run_on_main.py -q
"""

from __future__ import annotations

from typing import Callable

GhFn = Callable[..., dict | list | None]

# Единственные два статуса GitHub Compare API, при которых head_sha —
# предок base (или сам base) — см. докстринг модуля.
_ON_BRANCH_STATUSES = ("identical", "behind")


def head_is_on_branch(compare_status: str) -> bool:
    """True — статус GitHub Compare API `{base}...{head}` означает, что
    `head` целиком поглощён `base` (тот же коммит или предок). Чистая
    функция над уже прочитанным статусом — тело мутационного доказательства
    (см. test_ci_run_on_main.py: сними это `in`, оставь только `True`)."""
    return compare_status in _ON_BRANCH_STATUSES


def run_is_on_main(repo: str, head_sha: str, gh: GhFn, main_ref: str = "main") -> bool:
    """True — `head_sha` прогона является предком `main_ref` (или равен ему)
    ПРЯМО СЕЙЧАС, по данным GitHub Compare API. False — прогон относится к
    ветке-кандидату (PR, `workflow_dispatch` на agent/*-ветке и т.п.) и не
    может служить доказательством состояния main, даже если
    `conclusion=success`.

    `gh` — то же соглашение, что `pulse_guard.gh`: вызывается с args для
    `gh api`, возвращает распарсенный JSON или бросает RuntimeError на
    сетевую/HTTP-ошибку — здесь она не глушится, а поднимается дальше
    (fail loud, не silent-wrong: не смогли спросить GitHub — не решаем
    «предок» по умолчанию ни в одну из сторон)."""
    if head_sha == main_ref:
        return True
    payload = gh(f"repos/{repo}/compare/{main_ref}...{head_sha}")
    if not isinstance(payload, dict) or "status" not in payload:
        raise RuntimeError(
            f"compare {main_ref}...{head_sha}: ответ без status ({payload!r}) — "
            "предок не установлен, не гадаем")
    return head_is_on_branch(payload["status"])


def latest_run_on_main(runs: list[dict], repo: str, gh: GhFn, main_ref: str = "main") -> dict | None:
    """Первый прогон из уже полученного списка (GitHub отдаёт от нового к
    старому, порядок не меняем), чей `head_sha` реально предок `main_ref`.
    Прогоны на ветках-кандидатах (PR, ручной `workflow_dispatch` на agent/*)
    пропускаются, даже если формально зелёные — иначе именно они маскируют
    красное состояние main (живой случай #925, см. докстринг модуля).

    Прогон без `head_sha` в записи пропускается молча (неполная запись API —
    не повод поднимать RuntimeError, просто не кандидат)."""
    for run in runs:
        head_sha = run.get("head_sha")
        if not head_sha:
            continue
        if run_is_on_main(repo, head_sha, gh, main_ref):
            return run
    return None
