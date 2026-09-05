#!/usr/bin/env python3
"""Детерминированное ревью PR: первый гейт конвейера.

Срабатывает на каждый PR (workflow pr-review). Результат — метка-вердикт,
которую оркестратор использует как условие слияния:
  review:ok                 — замечаний нет, PR может быть слит
  review:changes-requested  — есть находки, автору нужно доработать

Проверки:
  1. Секреты в добавленных строках диффа (репозиторий публичный!): PAT GitHub,
     AWS, Slack, приватные ключи, присваивания длинных литералов *TOKEN/SECRET/KEY.
  2. В PR не добавлены файлы секретов (.dev.vars, .env).
  3. Крупный дифф (> LARGE_DIFF_LINES) помечается меткой review:large — авто-слияние
     для него запрещено, нужен взгляд. Взгляд делегирован AI-ревью (#204):
     после вердикта ai:ok на том же head AI-ревью само ставит review:large-ok
     (scripts/review/ai_review.py::cmd_verdict). Диффы длиннее LARGE_DIFF_HUGE_LINES
     автоматика не подтверждает вовсе — эскалирует владельцу.
  4. Каждый запуск проверяет вердикт второго гейта (AI-ревью, #18) против
     ТЕКУЩЕГО диффа PR (#252): дифф относительно base не изменился с момента
     последнего вердикта (отпечаток совпал — review_labels.diff_fingerprint,
     сохранён в поле `diff:` шапки комментария ai_review.build_comment) —
     ai:*-метка сохраняется, повторное дорогое AI-ревью не нужно. Подтягивание
     main без конфликтов меняет только head (merge-коммит), не патчи PR.
     Дифф действительно изменился (реальная правка), или отпечаток не удалось
     прочитать (старый комментарий без поля diff, сеть отказала) — метка
     снимается, как раньше; свежую поставит новое AI-ревью (workflow ai-review).

Среда: runner с `gh`, GH_TOKEN с правами pull-requests: write.
"""

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# Метки-вердикты обоих гейтов — одно место правды в scripts/lib
# (общее для check_pr, ai_review и scheduler: имена и гейт слияния).
_LIB = Path(__file__).resolve().parents[1] / "lib" / "review_labels.py"
_spec = importlib.util.spec_from_file_location("review_labels", _LIB)
review_labels = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(review_labels)

# Второй, более высокий порог: дифф длиннее него не подтверждается автоматикой
# даже при одобренном AI-ревью (#204) — эскалируется владельцу. Значение —
# запас над максимумом легитимных диффов, реально слитых в этом репозитории
# (замер 2026-09-02 по 32 PR: крупнейшие принятые — #137 1563 строк, #159 1127,
# #167 876; следующая заметная ступень — 762–787). 2000 оставляет весь
# наблюдаемый диапазон review:large под автоматикой и ловит только диффы
# заметно крупнее любого прецедента.
LARGE_DIFF_LINES = 800
LARGE_DIFF_HUGE_LINES = 2000
SECRET_PATTERNS = [
    (r"gh[pousr]_[A-Za-z0-9]{30,}", "GitHub PAT"),
    (r"github_pat_[A-Za-z0-9_]{20,}", "GitHub fine-grained PAT"),
    (r"AKIA[0-9A-Z]{16}", "AWS access key"),
    (r"xox[bposa]-[A-Za-z0-9-]{10,}", "Slack token"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "приватный ключ"),
    (r"(?:TOKEN|SECRET|KEY|PASSWORD|PASSWD)\s*[=:]\s*['\"][A-Za-z0-9+/_-]{20,}['\"]", "литерал секрета в присваивании"),
]
CONFLICT_MARKER = re.compile(r"^(<{7}|={7}|>{7})($| )")
FORBIDDEN_FILES = (".dev.vars", ".env")
REVIEW_OK = review_labels.REVIEW_OK
REVIEW_CHANGES = review_labels.REVIEW_CHANGES
REVIEW_LARGE = review_labels.REVIEW_LARGE
LARGE_OK = review_labels.LARGE_OK


def gh(*args: str) -> dict | list:
    result = subprocess.run(
        ["gh", "api", *args],
        capture_output=True, text=True,
        env={**os.environ, "NO_COLOR": "1"},
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh api {' '.join(args[:2])}: {result.stderr.strip()}")
    return json.loads(result.stdout)


def run_gh(*args: str) -> None:
    result = subprocess.run(
        ["gh", *args],
        capture_output=True, text=True,
        env={**os.environ, "NO_COLOR": "1"},
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:2])}: {result.stderr.strip()}")


def added_lines(diff: str) -> list[str]:
    return [line[1:] for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++")]


def size_gate(added: int, labels) -> tuple[bool, bool]:
    """Размерный гейт (#90): `(size_overflow, is_large)`.

    size_overflow — дифф крупнее LARGE_DIFF_LINES; is_large — крупный И НЕ
    принят меткой LARGE_OK. Единственное место формулировки условия: раньше
    оно жило инлайном в main(), и именно здесь (#90) стоял NameError — на
    маленьких диффах короткое замыкание `and` не доставало до неопределённого
    имени, поэтому баг молча ждал первого крупного PR. Тест кормится этой
    функцией (прод-форма: имена меток из review_labels), а не пересказом.
    """
    size_overflow = added > LARGE_DIFF_LINES
    return size_overflow, size_overflow and LARGE_OK not in labels


def large_acceptance_message(added: int, size_overflow: bool, is_large: bool) -> str | None:
    """«Принят меткой» — только когда метка review:large-ok ЕСТЬ.

    Задача #90 назвала это условие перевёрнутым («печатается, когда метки
    нет») — проверка случаями показывает обратное: is_large уже включает
    «метки нет», поэтому `size_overflow and not is_large` истинно ровно при
    наличии метки (прод-пруф: прогон 33693163400, PR #159 +1127 строк,
    печатает «принят меткой review:large-ok» и завершается review:ok).
    Условие зафиксировано функцией, чтобы правка по мотивам #90 не
    перевернула его молча.
    """
    if size_overflow and not is_large:
        return f"review: крупный дифф (+{added}) принят меткой {LARGE_OK}"
    return None


def verdict_for(is_large: bool, findings: list[str]) -> str:
    """Вердикт-метка: размер не принят → review:large — гейт размера падает
    (merge_label_gate не видит review:ok, слияние закрыто), иначе — по находкам.
    """
    if is_large:
        return REVIEW_LARGE
    return REVIEW_OK if not findings else REVIEW_CHANGES


def ai_verdict_keep(current_labels, stored_fp: str | None, current_fp: str) -> bool:
    """True — на PR есть хотя бы одна ai:*-метка (review_labels.
    ai_verdicts_to_drop не пуст), и её отпечаток диффа (#252) совпадает с
    сохранённым в последнем ревью-комментарии AI: метку снимать не нужно,
    дифф относительно base тот же, что ревьюили. Иначе (меток нет, дифф
    изменился, отпечаток отсутствует/не прочитан) — False, метка снимается,
    как раньше — при сомнении гейт не ослабляется (AGENTS.md).
    """
    return bool(review_labels.ai_verdicts_to_drop(current_labels)) and \
        review_labels.diff_unchanged(stored_fp, current_fp)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--tree", default=".",
                        help="каталог дерева PR для компиляционной проверки "
                             "(workflow pr-review кладёт его в pr-tree; сам "
                             "скрипт исполняется из чекаута main — дерево "
                             "данные, не код)")
    args = parser.parse_args()
    repo = os.environ["GITHUB_REPOSITORY"]

    diff = subprocess.run(
        ["gh", "pr", "diff", str(args.pr)],
        capture_output=True, text=True,
        env={**os.environ, "NO_COLOR": "1"},
    ).stdout

    findings: list[str] = []

    for i, line in enumerate(added_lines(diff)):
        for pattern, kind in SECRET_PATTERNS:
            if re.search(pattern, line):
                findings.append(f"Похоже на {kind} в добавленной строке {i + 1}: «{line.strip()[:60]}…»")
                break
        if CONFLICT_MARKER.match(line):
            findings.append(f"Неразрешённый конфликт-маркер уехал в коммит (строка {i + 1}).")

    # Постранично (#294): первая страница молча теряет хвост у PR за сотню
    # файлов — правка файла за сотым не меняла бы ни отпечаток диффа, ни
    # сумму added (review_labels.list_pr_files — одно место правды).
    files = review_labels.list_pr_files(repo, args.pr, gh)
    for f in files:
        name = f["filename"]
        if name.endswith(FORBIDDEN_FILES) or name in FORBIDDEN_FILES:
            findings.append(f"Файл секретов в PR: {name}")

    added = sum(f["additions"] for f in files)
    # Размерный гейт (> LARGE_DIFF_LINES): авто-слияние запрещено, нужен взгляд.
    # По политике 2026-08-30 «взгляд человека» делегирован ревью-агентам и
    # главному агенту: их вердикт публикуется в PR, после чего размер принимается
    # меткой review:large-ok — видимый в истории осознанный обход (паттерн
    # orchestra:skip). С #204 эту метку ставит сама автоматика AI-ревью после
    # вердикта ai:ok (scripts/review/ai_review.py::cmd_verdict), а не человек —
    # см. LARGE_DIFF_HUGE_LINES там же для предохранителя гигантских диффов.
    # Остальные проверки метка не отключает.
    pull = gh(f"repos/{repo}/pulls/{args.pr}")
    current = {label["name"] for label in pull["labels"]}
    size_overflow, is_large = size_gate(added, current)
    message = large_acceptance_message(added, size_overflow, is_large)
    if message:
        print(message)

    # Каждый изменённый .py обязан компилироваться: ловит обрезанные файлы и
    # неразрешённые конфликты, которые ломают скрипты молча (случалось с scheduler.py).
    # Компиляция — парс, не исполнение: дерево PR остаётся данными. Файла может
    # не быть в чекауте дерева (удалён в PR) — это не ошибка компиляции.
    import py_compile
    for f in files:
        name = f["filename"]
        local = os.path.join(args.tree, name)
        if name.endswith(".py") and name.startswith("scripts/") and os.path.exists(local):
            try:
                py_compile.compile(local, doraise=True)
            except py_compile.PyCompileError as error:
                findings.append(f"{name} не компилируется: {error.msg}")

    # Вердикт-метка: старые вердикты снимаются, вешается актуальный.
    for old in (REVIEW_OK, REVIEW_CHANGES):
        if old in current:
            run_gh("api", "-X", "DELETE", f"repos/{repo}/issues/{args.pr}/labels/{old}")
    # Вердикт AI-ревью (второй гейт, #18) привязан к head, который ревьюили:
    # этот скрипт выполняется на каждый пуш и обязан снять протухший ai:* ДО
    # нового AI-ревью — иначе оркестратор может слить PR по метке от старого
    # head. ИСКЛЮЧЕНИЕ (#252): если дифф PR относительно base не изменился с
    # момента вердикта (сверка отпечатков, ai_verdict_keep) — подтягивание
    # main без конфликтов меняет только head, метка остаётся в силе, лишний
    # дорогой прогон ai-review не нужен. Отпечаток не удалось прочитать (нет
    # комментария, старый формат без diff:, сеть отказала) — трактуем как
    # «изменился»: метка снимается, как раньше.
    current_fp = review_labels.diff_fingerprint(files)
    to_drop = review_labels.ai_verdicts_to_drop(current)
    stored_fp = None
    if to_drop:  # нечего сверять, если ai:*-метки на PR ещё нет (первое ревью)
        try:
            ai_comment = review_labels.latest_ai_comment(repo, args.pr, gh)
        except RuntimeError as error:
            print(f"::warning::review: не удалось прочитать вердикт AI для сверки диффа ({error}) — считаю дифф изменившимся")
            ai_comment = None
        stored_fp = review_labels.header_facts(ai_comment.get("body") or "").get("diff") if ai_comment else None
    if ai_verdict_keep(current, stored_fp, current_fp):
        print(f"review: дифф не изменился с прошлого вердикта AI ({current_fp[:12]}…) — {', '.join(to_drop)} сохранены")
        # Зеркало на новый head (находка ai-ревью PR #346): пока метка сохраняется,
        # ai-review.yml сам эту ветку не проходит — should_run_ai_review отдаёт
        # false, job verdict скипается целиком, и harness/ai-review на текущем SHA
        # никогда не появляется. Без этой публикации PR застревал бы в required
        # status checks в состоянии «Expected» навсегда после включения контекста
        # (тормоз без газа) — ai_comment уже прочитан выше для сверки отпечатка,
        # второй сетевой запрос не нужен, решение то же самое, что и у метки.
        reviewer = review_labels.header_facts(ai_comment.get("body") or "").get("reviewer") if ai_comment else None
        if reviewer:
            review_labels.post_commit_status(
                repo, pull["head"]["sha"], review_labels.STATUS_AI_REVIEW,
                review_labels.ai_status_state(reviewer),
                f"ai-review: {reviewer} (вердикт сохранён, дифф не изменился)",
                run_gh, review_labels.run_target_url(repo))
        else:
            print("::warning::review: дифф не изменился, но вердикт AI не удалось прочитать — harness/ai-review не обновлён на новом head")
    else:
        for old in to_drop:
            run_gh("api", "-X", "DELETE", f"repos/{repo}/issues/{args.pr}/labels/{old}")
    verdict = verdict_for(is_large, findings)
    run_gh("api", "-X", "POST", f"repos/{repo}/issues/{args.pr}/labels", "-f", f"labels[]={verdict}")

    # Commit Status API — тот же вердикт вторым каналом, параллельно метке
    # (#345): allow_auto_merge (уже включён на репозитории) читает required
    # status checks, не метки. Состояние вычислено из ТОЙ ЖЕ переменной
    # verdict, что и метка выше — второго источника истины не заводим.
    review_labels.post_commit_status(
        repo, pull["head"]["sha"], review_labels.STATUS_REVIEW,
        review_labels.review_status_state(verdict),
        f"review: {verdict}" + (f" (+{added} строк)" if not findings else f" ({len(findings)} находок)"),
        run_gh, review_labels.run_target_url(repo))

    if findings:
        body = "Ревью нашло замечания:\n" + "\n".join(f"- {f}" for f in findings)
        run_gh("api", "-X", "POST", f"repos/{repo}/issues/{args.pr}/comments", "-f", f"body={body}")
        for f in findings:
            print(f"::error::{f}")
        print(f"review: FAIL ({verdict})")
        return 1

    print(f"review: OK ({verdict}, +{added} строк)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as error:
        print(f"::error::review: {error}")
        sys.exit(1)
