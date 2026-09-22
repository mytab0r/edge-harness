#!/usr/bin/env python3
"""Живая проверка заявлений автора PR (#968) — glue-скрипт вокруг чистой
логики `mutation_claim.py`. Смотри докстринг того модуля для контракта
блоков «## Доказательство мутацией» / «## Класс закрыт» / «## Непроверено
машиной».

## Прогон ДО пуша — дешевле цикла CI

Скрипт читает тело PR из `GITHUB_EVENT_PATH`, и файл по этому пути может
быть собран руками. То есть автор проверяет свои заявления локально, не
тратя прогон раннера:

    python3 - <<'EOF'
    import json, pathlib
    body = pathlib.Path("ЧЕРНОВИК-ТЕЛА.md").read_text(encoding="utf-8")
    pathlib.Path("/tmp/evt.json").write_text(
        json.dumps({"pull_request": {"number": 1435, "body": body}}), encoding="utf-8")
    EOF
    GITHUB_EVENT_NAME=pull_request GITHUB_EVENT_PATH=/tmp/evt.json \
      GITHUB_REPOSITORY=<owner>/<repo> python scripts/lib/pr_mutation_claim_check.py

Требования рецепта, оплаченные находкой ai-review PR #1435 (круг 3; прежняя
редакция писала `"number": 0`, а `if not number:` принимает ноль за
ОТСУТСТВИЕ номера — скрипт выходил «неожиданная форма события», не проверив
тело вовсе): (1) номер — СУЩЕСТВУЮЩЕГО PR, список изменённых файлов читается
по сети `gh api .../pulls/<номер>/files`; у черновика тела, у которого PR ещё
нет, рабочего номера не существует в принципе; (2) нужен авторизованный `gh`
(GH_TOKEN) — без него проверки 1–2 (по телу, без сети) исполняются, а третья
громко отказывается на списке файлов. «Две секунды вместо шести минут» — про
проверку тела и мутаций у существующего PR, не про черновик без PR.

Зачем это написано здесь. Возможность существовала и раньше, но нигде не
названа — и каждая рассинхронизация тела стоила полного цикла CI плюс
лишнего коммита (тело, правленное без пуша, прогон не перезапускает).
Живой случай, которым абзац оплачен: PR #1435, прогон 2026-09-22 04:05 —
строка «Тест:» называла `test_main_runs_the_closed_task_sweep_every_pulse`,
а блок ```diff``` нёс СОВСЕМ ДРУГУЮ мутацию (перестановку порядка
mark/clear). Автор правил прозу и патч в разное время; гейт ответил честно
(«заявление ЛОЖНО: тест остался зелёным»), но ответ пришёл через шесть
минут раннера вместо двух секунд локального прогона. Утверждение о готовом
артефакте обязано нести его адрес (AGENTS.md) — вот адрес.

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
  3. **«Мутация доказана»** — проверяется ВСЕГДА, когда блок присутствует
     в теле PR: автор написал блок — он заявил, проверка за этим следует
     (находка ai-review PR #1028: ограничение «только PR с гвардиями»
     пропускало мотивирующий случай — ложное «доказательство мутацией»
     коммита 508e6899 из #893 лежало в PR, трогавшем только
     `scripts/git/worktree-cleanup.py` и
     `scripts/lib/test_worktree_cleanup_guard.py`, и мимо собственно
     механизма #968 прошло бы молча). ОБЯЗАТЕЛЬНЫМ блок держится только
     для PR, меняющих `scripts/ci/guards/*.sh` (см.
     `mutation_claim.guard_catalog_paths_changed`): дорогая проверка
     (применяет патч, дважды гоняет pytest, откатывает) ложится на автора
     только тогда, когда он сам заявил мутацию или добавляет/меняет гвардию
     каталога. Блокирует: у заявленной мутации обязан быть вердикт `proved`;
     у обязательной — само её наличие. Удаление гвардии каталога не
     блокирует (патчу нечего снимать), но печатает отдельную строку —
     решение за ревью.

Вход: событие `pull_request` (тело PR, номер — из $GITHUB_EVENT_PATH, без
`gh api`); список изменённых файлов — `gh api .../pulls/{n}/files`
(GH_TOKEN уже в env на уровне шага-перебора каталога гвардий, #749, см.
repo-ci.yml). На событиях, отличных от pull_request (push, workflow_dispatch)
— проверка пропускается целиком с явной причиной (тела PR нет), это не сбой.

Замер цены обязательного блока — командой, не по памяти (числа зависят от
окна и базы: находка ai-review PR #1028 показала, что прежние «12 из 328 за
окно» по памяти не воспроизводятся). На базе этой ветки dddd94c вся история —
343 коммита, из них 19 трогают `scripts/ci/guards/*.sh` (~5,5%):

    git log dddd94c --format='%H' -- 'scripts/ci/guards/*.sh' | wc -l   # 19
    git rev-list --count dddd94c                                        # 343

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

# --- rate_guard: исчерпанный бюджет API — предупреждение, не красный required-гейт (#1004) ---
_rate_guard_spec = importlib.util.spec_from_file_location(
    "rate_guard", Path(__file__).resolve().parent / "rate_guard.py")
_rate_guard = importlib.util.module_from_spec(_rate_guard_spec)
_rate_guard_spec.loader.exec_module(_rate_guard)
# --- конец rate_guard ---

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

# Честный газ тормоза (находка ai-review PR #1028): repo-ci.yml триггерится
# на голый `pull_request:` без `edited`, поэтому САМА правка тела PR
# обязательную проверку не перезапускает. Каждое сообщение, починка которого
# требует правки тела, обязано называть рабочий путь: новый коммит (любой) —
# или переоткрытие PR. Прецедент триггера с `edited` — orchestra.yml (#599);
# подключать `edited` к repo-ci здесь сознательно не делаем: каждая правка
# тела перезапускала бы весь job `test` и цепочку ai-review — отдельное
# решение о цене, не побочный эффект этого механизма.
GAS_NOTE = (
    "Газ: добавь/исправь блок в теле PR и запуши любой коммит (одна правка "
    "тела прогон repo-ci не перезапускает — триггер без `edited`), либо "
    "переоткрой PR"
)


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


def list_pr_changed_and_removed_files(repo: str, number: int) -> tuple[list[str], list[str]]:
    """(изменённые, удалённые) пути файлов PR, постранично (класс #308 —
    не читать только первую страницу). Удалённые (`status == "removed"`)
    НЕ выбрасываются молча: PR, целиком убивающий гвардию каталога, —
    крайнее значение объекта, и раньше оно было невидимо в логе вовсе
    (находка ai-review PR #1028)."""
    changed: list[str] = []
    removed: list[str] = []
    page = 1
    while True:
        chunk = _gh_api(f"repos/{repo}/pulls/{number}/files?per_page=100&page={page}") or []
        for entry in chunk:
            if entry.get("status") == "removed":
                removed.append(entry["filename"])
            else:
                changed.append(entry["filename"])
        if len(chunk) < 100:
            break
        page += 1
    return changed, removed


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
        f"перечислив, что именно непроверено. {GAS_NOTE}"
    )
    return True


def _run_class_closed(body: str, repo_root: Path) -> bool:
    """True — есть провал (заявленное число не совпало с фактом)."""
    try:
        claims = mutation_claim.parse_class_closed_claims(body)
    except mutation_claim.MutationClaimFormatError as error:
        print(f"::error::{error} {GAS_NOTE}")
        return True
    failed = False
    for claim in claims:
        try:
            ok, matches = mutation_claim.run_class_closed_check(repo_root, claim)
        except RuntimeError as error:
            # rc >= 2 у git grep (битый ERE, не-репозиторий) — ошибка ФОРМЫ
            # заявления, а не «не совпало». Раньше это роняло glue
            # необработанным трейсбеком ДО мутационной фазы: GAS_NOTE не
            # печатался вовсе — единственный путь отказа без газа (находка
            # ai-review PR #1028, чеклист). Громко, с газом, и мутационная
            # фаза после этого продолжает работу.
            failed = True
            print(
                f"::error::mutation-claim[класс закрыт]: паттерн «{claim.pattern}» "
                f"не исполняется: {error} {GAS_NOTE}"
            )
            continue
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


def _run_mutation_proof(
    body: str, changed_files: list[str], removed_files: list[str], repo_root: Path
) -> bool:
    """True — есть провал (заявленная мутация не доказана / обязательный блок
    отсутствует / блок неверной формы). Печатает, что проверяется, а что нет —
    молчаливого пропуска быть не должно.

    Заявление проверяется ВСЕГДА, когда блок присутствует в теле: автор
    написал блок — он заявил, и мотивирующий случай (#893: ложное
    «доказательство мутацией» в PR, НЕ трогающем каталог гвардий) проходит
    мимо механизма только при ограничении «сначала посмотри файлы PR»
    (находка ai-review PR #1028). Обязательным блок остаётся только для PR,
    меняющих `scripts/ci/guards/*.sh` — цена на тех, кто не заявлял, не
    ложится."""
    guard_paths = mutation_claim.guard_catalog_paths_changed(changed_files)
    removed_guards = mutation_claim.guard_catalog_paths_changed(removed_files)
    if removed_guards:
        # Не блокирует (патчу нечего снимать), но и не молчит: удаление
        # гвардии — крайнее значение объекта, решение за ревью (находка
        # ai-review PR #1028).
        print(
            f"::warning::mutation-claim: PR УДАЛЯЕТ гвардию(и) каталога: "
            f"{removed_guards} — мутационная проверка на удаление не "
            f"запускается (снимать нечего), решение за ревью"
        )
    if guard_paths:
        print(f"mutation-claim: PR меняет гвардию(и) каталога: {guard_paths} — требуется «{mutation_claim.MUTATION_HEADING}»")

    try:
        claims = mutation_claim.parse_mutation_claims(body)
    except mutation_claim.MutationClaimFormatError as error:
        print(f"::error::{error} {GAS_NOTE}")
        return True

    if not claims:
        if guard_paths:
            print(
                f"::error::mutation-claim: PR меняет {guard_paths}, но тело PR не "
                f"несёт блока «{mutation_claim.MUTATION_HEADING}» — заявление о "
                f"мутации обязательно для PR, добавляющего/меняющего гвардию каталога "
                f"scripts/ci/guards/*.sh (issue #968). {GAS_NOTE}"
            )
            return True
        print(
            "mutation-claim: PR не меняет scripts/ci/guards/*.sh, блок "
            "«Доказательство мутацией» в теле отсутствует — мутационная "
            "проверка не требуется (ограничение цены, #968)"
        )
        return False

    failed = False
    for claim in claims:
        outcome = mutation_claim.run_mutation_proof(repo_root, claim)
        print(outcome.report())
        if outcome.verdict != "proved":
            failed = True
            print(f"mutation-claim: {GAS_NOTE}")
        if outcome.tree_restored is False:
            # Не продолжаем по мутированному дереву: следующий прогон дал бы
            # вердикт по чужой причине (данные различают, не догадка —
            # mutation_claim.MutationProofOutcome.tree_restored).
            print(
                "::error::mutation-claim: дерево могло остаться мутированным "
                "после этого заявления — остальные заявления этого прогона "
                "не проверяются (продолжение дало бы вердикты по чужой "
                "причине); исправь патч и запуши заново"
            )
            break
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
        changed_files, removed_files = list_pr_changed_and_removed_files(repo, number)
    except GhError as error:
        print(f"::error::mutation-claim: список файлов PR #{number} не прочитан ({error})")
        return 1

    failed = _run_mutation_proof(body, changed_files, removed_files, REPO_ROOT) or failed

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_rate_guard.run_guard_main(main, guard='mutation-claim-guard'))
