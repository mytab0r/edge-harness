#!/usr/bin/env python3
"""AI-ревью диффа: второй гейт конвейера после детерминированного ревью (#18).

Trust-зона разрезана по шагам workflow ai-review:

  gather  (доверенный, GH_TOKEN)   — факты PR, дифф-пак, задача из пула,
                                      промпт по шаблону scripts/review/ai_prompt.md
  DSH     (НЕдоверенный, без токена) — агент читает репозиторий и дифф-пак,
                                      отвечает текстом; постить в GitHub не может
                                      физически: у шага нет ни GH_TOKEN, ни git-креденшелов
  verdict (доверенный, GH_TOKEN)   — разбор ответа по контракту, комментарий
                                      в PR, метка-вердикт ai:ok /
                                      ai:changes-requested / ai:failed

Контракт ответа — как у живого решения владельца в Harness (pr_loop.py):
единственный сигнал вердикта — машиночитаемая ПОСЛЕДНЯЯ строка
«ВЕРДИКТ: approve|rework»; маркера нет, их два или он не последний —
error, неоднозначность никогда не одобряет.

Вердикт привязан к head: если PR успел получить новый пуш, вердикт не
применяется (метка/комментарий не ставятся) — новый пуш сам заведёт свежее
ревью, а детерминированное ревью к тому же снимает старые ai:*-метки.

Состояние для «завести задачи в беклог одной командой» живёт в комментарии:
шапка-факты (pr/head/reviewer) до первого пустой строки + канонические
блоки-заборы ````задача — парсит scripts/review/file_tasks.py.

Среда: runner с gh, GH_TOKEN с правами pull-requests: write (gather/verdict).
"""

import argparse
import importlib.util
import json
import os
import re
import string
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

# Метки-вердикты — одно место правды в lib (общее для check_pr/scheduler).
_LIB = SCRIPT_DIR.parent / "lib" / "review_labels.py"
_spec = importlib.util.spec_from_file_location("review_labels", _LIB)
review_labels = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(review_labels)

AI_OK = review_labels.AI_OK
AI_CHANGES = review_labels.AI_CHANGES
AI_FAILED = review_labels.AI_FAILED
AI_VERDICTS = review_labels.AI_VERDICTS

# Номер задачи из текста PR/issue — одно место правды (#187): границы числа
# с обеих сторон, не подстрока (класс «#18 совпал с #180» на contract_check,
# 33570081734).
_tr_spec = importlib.util.spec_from_file_location(
    "task_ref", SCRIPT_DIR.parent / "lib" / "task_ref.py")
task_ref = importlib.util.module_from_spec(_tr_spec)
_tr_spec.loader.exec_module(task_ref)

# Контракт ответа модели. Строка ВЕРДИКТ обязана быть последней непустой и
# единственной — двусмысленность это error, а не одобрение. Модель периодически
# оборачивает машиночитаемую строку в markdown-выделение (**…**/__…__) вопреки
# промпту — это тот же сигнал, что и голая строка, парсер обязан его снять.
# Группа 1 — необязательный маркер, пустая альтернатива в её же группе
# (а не «?» снаружи) нужна, чтобы backreference \1 совпал с пустой строкой,
# когда обрамления нет вовсе. Открывающий и закрывающий маркер должны
# совпадать — «*ВЕРДИКТ: approve__» не становится валидной формой. Упоминание
# approve/rework ВНУТРИ строки прозы сюда не попадает — якоря ^…$ и жёсткая
# форма это исключают.
VERDICT_RE = re.compile(r"^(\*\*|__|)ВЕРДИКТ:\s*(approve|rework)\s*\.?\1$")
# Блок задачи в беклог: ЗАДАЧА: <заголовок> … КОНЕЦ ЗАДАЧИ. Незакрытый блок
# не принимается — тихо взять половину хуже, чем не взять совсем.
TASK_OPEN_RE = re.compile(r"^ЗАДАЧА:\s*(\S.*)$")
TASK_CLOSE = "КОНЕЦ ЗАДАЧИ"
# Блок-забор задачи в комментарии: строится только транспортом, парсится
# file_tasks. ЧЕТЫРЕ бэктика: внутренний ```-фенс в теле задачи (пример
# кода) не закрывает блок — иначе roundtrip молча обрезал бы тело.
TASK_FENCE = "````задача"
FENCE_CLOSE_RE = re.compile(r"^`{4,}\s*$")
# Шапка-факты комментария: разбираются только до первого пустой строки,
# чтобы проза/фенсы ниже не притворялись фактами.
FACT_RE = re.compile(r"^(pr|head|reviewer):\s*(.+)$")


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


def redact(text: str) -> str:
    """Маскирование секретов — ТО ЖЕ место правды, что у bash-транспортов:
    scripts/lib/dsh-ci.sh::redact. Вызывается subprocess'ом (sed-паттерны не
    дублируются на второй язык), отказ громкий."""
    result = subprocess.run(
        ["bash", "-c", f'source "{_LIB.parent / "dsh-ci.sh"}"; redact'],
        input=text, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"redact (dsh-ci.sh): {result.stderr.strip()}")
    return result.stdout


# ── Чистая логика: разбор ответа агента ──────────────────────────────────────

def parse_verdict(answer: str) -> str:
    """approve | rework | error. Единственный сигнал — машиночитаемая
    ПОСЛЕДНЯЯ строка; маркера нет, два или не последний — error."""
    lines = [line.strip() for line in (answer or "").splitlines() if line.strip()]
    marks = [m.group(2) for line in lines for m in [VERDICT_RE.match(line)] if m]
    if len(marks) == 1 and lines and VERDICT_RE.match(lines[-1]):
        return marks[0]
    return "error"


def transport_failed(dsh_rc: str) -> bool:
    """True — DSH не смог вызвать модель вовсе (rc≠0: сеть, 404, таймаут).

    Единственный источник истины — код возврата dsh (ai_dsh.sh пишет его в
    dsh_rc.txt, независимо от содержимого ответа). Пусто/не-число — код
    неизвестен (экзотический обрыв шага раннера) и по умолчанию НЕ считается
    транспортным сбоем: ложное «инфраструктура сломана» хуже, чем чуть менее
    точный «модель ответила не по контракту» в редком крайнем случае.
    """
    try:
        return int(dsh_rc) != 0
    except (TypeError, ValueError):
        return False


def error_reason(answer: str, dsh_rc: str) -> str:
    """Причина verdict=error — три состояния, не смешиваемые в одно (класс
    silent-wrong прогона 33572445063: ошибка провайдера читалась как «модель
    нарушила контракт»). Порядок проверки важен: транспорт — раньше формата,
    потому что при упавшем транспорте answer пуст и verdict_line_present
    всё равно вернёт False — не значит «модель промолчала»."""
    if transport_failed(dsh_rc):
        return f"ревью не состоялось — ошибка провайдера/транспорта DSH (код возврата {dsh_rc})"
    if verdict_line_present(answer):
        return "модель ответила, но строка «ВЕРДИКТ: …» есть, а не единственная и/или не последняя"
    return "модель ответила, но строки «ВЕРДИКТ: …» нет вообще"


def verdict_line_present(answer: str) -> bool:
    """Есть ли в ответе хоть одна строка, похожая на строку вердикта (в любом
    количестве и с любым обрамлением) — используется только для диагностики:
    различить «модель не написала вердикт вовсе» от «написала, но неоднозначно»
    в сообщении об ошибке. На сам вердикт не влияет — граница контракта не
    меняется, это чисто текст для человека."""
    lines = [line.strip() for line in (answer or "").splitlines() if line.strip()]
    return any(VERDICT_RE.match(line) for line in lines)


def parse_tasks(answer: str) -> list[dict]:
    """Блоки ЗАДАЧА: … КОНЕЦ ЗАДАЧИ из ответа. Незакрытый/пустой блок
    отбрасывается целиком: полузадача в пуле хуже отсутствия задачи."""
    tasks: list[dict] = []
    title: str | None = None
    body: list[str] = []
    for line in (answer or "").splitlines():
        stripped = line.strip()
        if title is None:
            match = TASK_OPEN_RE.match(stripped)
            if match:
                title = match.group(1).strip()
                body = []
        elif stripped == TASK_CLOSE:
            tasks.append({"title": title, "body": "\n".join(body).strip()})
            title, body = None, []
        else:
            body.append(line.rstrip())
    return tasks


def findings_of(answer: str, tasks: list[dict] | None = None) -> str:
    """Проза ответа без строк вердикта и блоков задач: маркер вердикта
    отражается в reviewer:, задачи переезжают в канонические фенсы."""
    tasks = tasks if tasks is not None else parse_tasks(answer)
    titles = {t["title"] for t in tasks}
    lines: list[str] = []
    in_task = False
    for line in (answer or "").splitlines():
        stripped = line.strip()
        if VERDICT_RE.match(stripped):
            continue
        if in_task:
            if stripped == TASK_CLOSE:
                in_task = False
            continue
        match = TASK_OPEN_RE.match(stripped)
        if match and match.group(1).strip() in titles:
            in_task = True
            continue
        lines.append(line.rstrip())
    return "\n".join(lines).strip("\n").strip()


def build_comment(number: int, sha: str, verdict: str, findings: str,
                  tasks: list[dict]) -> str:
    """Канонический комментарий-вердикт. Шапка-факты — САМЫЕ ПЕРВЫЕ строки,
    до первого пустой строки (инвариант: file_tasks.py парсит ТОЛЬКО эту
    зону и фенсы задач, проза и заборы не могут притвориться фактами)."""
    head = (
        f"pr: {number}\nhead: {sha}\nreviewer: {verdict}\n\n"
        f"🤖 AI-ревью — второй гейт конвейера (#18). Вердикт: {verdict}."
    )
    body = findings.strip()
    if tasks:
        close = "`" * len(TASK_FENCE[: TASK_FENCE.index("з")])  # ровно столько же бэктиков, сколько в открывающем
        blocks = "\n\n".join(
            f"{TASK_FENCE}\n{t['title']}\n{t['body']}\n{close}" for t in tasks
        )
        body += (
            f"\n\nЗадачи в беклог из этого ревью — завести одной командой:\n"
            f"    python scripts/review/file_tasks.py --pr {number}\n\n{blocks}"
        )
    return f"{head}\n\n{body}\n".strip() + "\n"


def header_facts(comment_body: str) -> dict[str, str]:
    lines = (comment_body or "").splitlines()
    facts: dict[str, str] = {}
    for line in lines:
        if not line.strip():
            break  # шапка кончилась: дальше проза и фенсы, не факты
        match = FACT_RE.match(line.strip())
        if match:
            facts[match.group(1)] = match.group(2).strip()
    return facts


def tasks_from_comment(comment_body: str) -> list[dict]:
    """Канонические фенсы задач из комментария ревью (не из сырого ответа):
    комментарий — долговременное место правды для file_tasks.py. Закрывается
    строкой из ≥4 бэктиков: внутренний ```-фенс остаётся телом задачи."""
    tasks: list[dict] = []
    lines = (comment_body or "").splitlines()
    i = 0
    while i < len(lines):
        if lines[i].strip() == TASK_FENCE:
            block: list[str] = []
            i += 1
            while i < len(lines) and not FENCE_CLOSE_RE.match(lines[i].strip()):
                block.append(lines[i].rstrip())
                i += 1
            if block and i < len(lines):  # забор закрыт
                tasks.append({
                    "title": block[0].strip(),
                    "body": "\n".join(block[1:]).strip(),
                })
        i += 1
    return [t for t in tasks if t["title"]]


# ── gather: факты, дифф-пак, промпт ──────────────────────────────────────────

def is_not_found(error: RuntimeError) -> bool:
    """«Запрошенной issue нет» — по ТОЧНОЙ форме gh «Not Found (HTTP 404)»,
    не по подстроке «404»: она ловит и URL (issues/404), из-за чего отказ
    сети/права по задаче с «404» в номере молчно считался бы «не задача»."""
    return "HTTP 404" in str(error)


def task_section(pull_body: str, repo: str) -> str:
    """Задача пула, которую закрывает PR: первая открытая issue с меткой task
    из #N-ссылок тела. Нет задачи (orchestra:skip, dependabot) — так и пишем:
    «нет задачи» и «задача не дочиталась» — разные состояния: 404 значит
    «не задача, ищем дальше», любой другой отказ (права, сеть, 5xx) роняет
    шаг громко — молча ревьюить без контекста задачи нельзя (silent-wrong).
    """
    for number in sorted(set(task_ref.extract_task_refs(pull_body or ""))):
        try:
            issue = gh(f"repos/{repo}/issues/{number}")
        except RuntimeError as error:
            if is_not_found(error):
                continue
            raise
        if "pull_request" in issue or issue.get("state") != "open":
            continue
        if "task" not in {label["name"] for label in issue["labels"]}:
            continue
        return (
            f"Задача из пула, которую закрывает этот PR: #{number} «{issue['title']}»\n\n"
            f"{issue.get('body') or ''}"
        )
    return (
        "Задача из пула: у PR нет открытой задачи с меткой task (orchestra:skip или "
        "сопровождение) — ревьюй по документации репозитория и здравому смыслу."
    )


def cmd_gather(args: argparse.Namespace) -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    pull = gh(f"repos/{repo}/pulls/{args.pr}")
    diff_run = subprocess.run(
        ["gh", "pr", "diff", str(args.pr)],
        capture_output=True, text=True,
        env={**os.environ, "NO_COLOR": "1"},
    )
    if diff_run.returncode != 0 or not diff_run.stdout.strip():
        # Пустой дифф-пак = ревью «ни о чём» с видом настоящего (silent wrong):
        # отказ громкий, шаг красный.
        raise RuntimeError(f"gh pr diff {args.pr}: rc={diff_run.returncode}, "
                           f"diff пуст: {diff_run.stderr.strip()[:200]}")
    diff = diff_run.stdout
    files = gh(f"repos/{repo}/pulls/{args.pr}/files?per_page=100")
    added = sum(f["additions"] for f in files)
    listing = "\n".join(f"{f['filename']} (+{f['additions']}/-{f['deletions']})" for f in files)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    pack = out / "pack.txt"
    pack.write_text(f"FILES:\n{listing}\n\nADDITIONS: {added}\n\nDIFF:\n{diff}", encoding="utf-8")

    template = string.Template((SCRIPT_DIR / "ai_prompt.md").read_text(encoding="utf-8"))
    prompt = template.safe_substitute(
        pr=args.pr,
        title=pull.get("title", ""),
        branch=pull["head"]["ref"],
        author=(pull.get("user") or {}).get("login", ""),
        context_pack=pack,
        task_section=task_section(pull.get("body") or "", repo),
    )
    (out / "prompt.md").write_text(prompt, encoding="utf-8")
    (out / "meta.json").write_text(
        json.dumps({"pr": args.pr, "head": pull["head"]["sha"]}), encoding="utf-8")
    print(f"gather: PR #{args.pr} head {pull['head']['sha'][:12]}, "
          f"+{added} строк, промпт {len(prompt)} байт, пак {pack}")
    return 0


# ── verdict: разбор ответа, комментарий, метка ────────────────────────────────

def cmd_verdict(args: argparse.Namespace) -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    answer = Path(args.answer).read_text(encoding="utf-8") if Path(args.answer).exists() else ""
    verdict = parse_verdict(answer)
    tasks = parse_tasks(answer)
    findings = redact(findings_of(answer, tasks))
    tasks = [{"title": redact(t["title"]).strip(), "body": redact(t["body"]).strip()}
             for t in tasks]
    tasks = [t for t in tasks if t["title"]]

    # Причина «error» — вычисляется ДО комментария: три разных состояния не
    # смешиваются ни в логе, ни в тексте для человека (silent-wrong класс:
    # ошибка провайдера не должна выглядеть как «модель ответила криво»).
    reason = error_reason(answer, args.dsh_rc) if verdict == "error" else None
    if reason and not findings.strip():
        findings = reason

    pull = gh(f"repos/{repo}/pulls/{args.pr}")
    if pull["head"]["sha"] != args.head:
        print(f"::warning::head PR #{args.pr} сменился ({args.head[:12]} → "
              f"{pull['head']['sha'][:12]}) — вердикт {verdict} не применяю: "
              f"новый пуш заведёт свежее ревью")
        return 0

    current = {label["name"] for label in pull["labels"]}
    for old in AI_VERDICTS:
        if old in current:
            run_gh("api", "-X", "DELETE", f"repos/{repo}/issues/{args.pr}/labels/{old}")
    label = AI_OK if verdict == "approve" else (AI_CHANGES if verdict == "rework" else AI_FAILED)
    run_gh("api", "-X", "POST", f"repos/{repo}/issues/{args.pr}/labels",
           "-f", f"labels[]={label}")

    body = build_comment(args.pr, args.head, verdict, findings, tasks)
    run_gh("api", "-X", "POST", f"repos/{repo}/issues/{args.pr}/comments",
           "-f", "body=" + body)

    if verdict == "error":
        tail = redact("\n".join((answer or "").splitlines()[-12:]))
        print(f"::error::ответ не соответствует контракту вердикта ({reason}) "
              f"— ai:failed, ревью повторится")
        print(f"хвост ответа:\n{tail}")
        return 1
    print(f"verdict: {verdict} — {label}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    gather = sub.add_parser("gather", help="факты PR + дифф-пак + промпт")
    gather.add_argument("--pr", type=int, required=True)
    gather.add_argument("--out", required=True)
    gather.set_defaults(func=cmd_gather)

    verdict = sub.add_parser("verdict", help="разбор ответа + комментарий + метка")
    verdict.add_argument("--pr", type=int, required=True)
    verdict.add_argument("--answer", required=True)
    verdict.add_argument("--head", required=True)
    # Код возврата dsh (ai_dsh.sh::dsh_rc.txt) — различает «транспорт упал»
    # от «дсш вернул текст не по контракту». Необязателен (default=""):
    # ручной запуск verdict без этого аргумента не должен падать — просто
    # теряет уточнение причины (см. transport_failed: пусто → не транспорт).
    verdict.add_argument("--dsh-rc", default="")
    verdict.set_defaults(func=cmd_verdict)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as error:
        print(f"::error::ai-review: {error}")
        sys.exit(1)
