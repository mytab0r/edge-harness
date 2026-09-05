#!/usr/bin/env python3
"""AI-ревью диффа: второй гейт конвейера после детерминированного ревью (#18).

Trust-зона разрезана по шагам workflow ai-review:

  should-run (доверенный, GH_TOKEN) — trusted-facts шаг ai-review.yml: PR уже
                                      прошёл review:ok, но дорогой прогон нужен,
                                      только если дифф действительно изменился
                                      с последнего вердикта (#294 — см. ниже)
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
шапка-факты (pr/head/reviewer/diff) до первого пустой строки + канонические
блоки-заборы ````задача — парсит scripts/review/file_tasks.py. Поле diff —
отпечаток диффа PR на момент вердикта (review_labels.diff_fingerprint,
#252): check_pr.py сверяет его с текущим и сохраняет ai:*-метку, если
подтягивание main не изменило дифф PR — см. review_labels.diff_unchanged.

Триггер ai-review.yml — workflow_run от pr-review, не метка (GITHUB_TOKEN не
создаёт событий по меткам): сохранённая check_pr.py метка сама по себе не
мешает workflow_run запуститься заново на чистом подтягивании main. Находка
вердикта AI-ревью PR #294: критерий приёмки «слияние одного PR не порождает
дорогих прогонов у остальных» не выполнялся, пока сверка отпечатка жила
только в check_pr.py. Чинит cmd_should_run/review_labels.should_run_ai_review
— то же место правды, что диффов diff_fingerprint/diff_unchanged, читаемое
шагом facts ai-review.yml ДО чекаута/gather/DSH: go=false — трудный прогон
не идёт вовсе, не просто «метка не переставляется». ai:failed из этого
пропуска исключён — его автоповтор по таймеру (#196) не должен зависеть от
неизменности диффа.

Тормоз/газ размерного гейта (#204): approve на том же head, что и review:large,
автоматически ставит review:large-ok (см. apply_large_ok/large_ok_decision) —
взгляд человека делегирован состоявшемуся вердикту AI, а не факту запуска.
Диффы длиннее check_pr.LARGE_DIFF_HUGE_LINES автоматика не подтверждает —
эскалирует владельцу через pulse_guard.escalate.

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

# Пороги размерного гейта (LARGE_DIFF_LINES/LARGE_DIFF_HUGE_LINES) — одно
# место правды в check_pr.py, рядом друг с другом (#204). Импорт по файлу
# (не как пакет) — тот же паттерн, что у review_labels/task_ref выше.
_cp_spec = importlib.util.spec_from_file_location(
    "check_pr", SCRIPT_DIR / "check_pr.py")
check_pr = importlib.util.module_from_spec(_cp_spec)
_cp_spec.loader.exec_module(check_pr)

# Канал эскалации владельцу (диффы сверх LARGE_DIFF_HUGE_LINES, #204) — тот же,
# что у предохранителя конвейера: комментарий в задачу-статус + Telegram
# (pulse_guard.escalate). Второго канала для класса «нужно решение владельца»
# не заводим (см. docstring escalate).
_pg_spec = importlib.util.spec_from_file_location(
    "pulse_guard", SCRIPT_DIR.parent / "orchestra" / "pulse_guard.py")
pulse_guard = importlib.util.module_from_spec(_pg_spec)
_pg_spec.loader.exec_module(pulse_guard)

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
# Шапка-факты комментария (pr/head/reviewer/diff) — одно место правды в
# review_labels.py (#252): check_pr.py читает ту же функцию, не вторую копию
# регэкспа, чтобы разбор поля diff не разошёлся между читателем и писателем.
FACT_RE = review_labels.FACT_RE
header_facts = review_labels.header_facts


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


# ── Размерный гейт: газ к тормозу review:large (#204) ─────────────────────────

def large_ok_decision(added: int, current_labels, verdict: str) -> str:
    """«ok» — можно автоматически поставить review:large-ok; «escalate» —
    дифф крупнее LARGE_DIFF_HUGE_LINES, решение за владельцем; «skip» —
    ничего не менять (дифф не review:large или AI не одобрил).

    Требует одобренного AI-вердикта на том же head (verdict == "approve"):
    подтверждение размера обязано опираться на состоявшийся разбор диффа,
    а не на факт запуска ревью (условие из #204, п.1) — иначе rework/error
    молча открывал бы газ тормозу, для которого он не предназначен.
    """
    names = review_labels._names(current_labels)
    if check_pr.REVIEW_LARGE not in names:
        return "skip"
    if verdict != "approve":
        return "skip"
    if added > check_pr.LARGE_DIFF_HUGE_LINES:
        return "escalate"
    return "ok"


def huge_diff_escalation_text(pr: int, added: int) -> str:
    """Текст эскалации гигантского диффа. Обязан заканчиваться разделом
    «что дальше» (требование владельца от 2026-09-02, #170) — констатация
    без плана не принимается."""
    return (
        f"🚨 edge-harness: PR #{pr} — дифф +{added} строк превышает второй "
        f"порог review:large-ok ({check_pr.LARGE_DIFF_HUGE_LINES}) — жду решения "
        "владельца по объёму.\n\n"
        "Автоматика AI-ревью одобрила дифф (ai:ok), но не подтверждает размер "
        f"сама: {check_pr.LARGE_DIFF_HUGE_LINES}+ строк — за пределами диапазона, "
        "который проверен на реальных PR этого репозитория (#204).\n\n"
        "Что дальше:\n"
        f"- Исполнитель: владелец репозитория.\n"
        "- Само по себе ничего не произойдёт — PR останется с review:large "
        "без review:large-ok, авто-слияние заблокировано.\n"
        f"- Нужно явное решение: поставить review:large-ok вручную, если объём "
        "оправдан, либо запросить разбивку PR на части."
    )


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
                  tasks: list[dict], diff_fp: str | None = None) -> str:
    """Канонический комментарий-вердикт. Шапка-факты — САМЫЕ ПЕРВЫЕ строки,
    до первого пустой строки (инвариант: file_tasks.py парсит ТОЛЬКО эту
    зону и фенсы задач, проза и заборы не могут притвориться фактами).

    diff_fp — отпечаток диффа PR на момент вердикта (review_labels.
    diff_fingerprint, #252): check_pr.py читает его из поля `diff:` шапки,
    чтобы решить, сохранять ли ai:*-метку при следующем пуше. Необязателен
    (None не добавляет строку) — не ломает старые вызовы/тесты, которые
    факта diff не ждут."""
    diff_line = f"diff: {diff_fp}\n" if diff_fp else ""
    head = (
        f"pr: {number}\nhead: {sha}\nreviewer: {verdict}\n{diff_line}\n"
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
    # Постранично (#294): review_labels.list_pr_files — то же место правды,
    # что и у check_pr.py; первая страница у PR за сотню файлов молча теряла
    # хвост (недосчёт added, невидимая правка для diff_fingerprint в verdict).
    files = review_labels.list_pr_files(repo, args.pr, gh)
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
    # Переходная совместимость: bridge на main (до мержа этого PR) берёт head
    # для сверки из meta.json; НОВЫЙ bridge берёт step-output фактов, а файл
    # остаётся диагностическим артефактом gather. Удалить вместе со старым
    # bridge после мержа (задача в беклоге).
    (out / "meta.json").write_text(
        json.dumps({"pr": args.pr, "head": pull["head"]["sha"]}), encoding="utf-8")
    print(f"gather: PR #{args.pr} head {pull['head']['sha'][:12]}, "
          f"+{added} строк, промпт {len(prompt)} байт, пак {pack}")
    return 0


# ── should-run: нужен ли дорогой прогон вообще (#294) ────────────────────────
#
# Вызывается из шага «trusted facts» самого ai-review.yml ДО чекаута
# pr-head/gather/DSH: PR уже прошёл проверку `review:ok` (её делает bash-код
# facts-шага), эта команда решает, оправдан ли дорогой вызов модели, ту же
# функцию, что читает check_pr.py для решения «сохранить ли метку»
# (review_labels.should_run_ai_review — одно место правды, а не вторая копия
# условия в YAML).

def cmd_should_run(args: argparse.Namespace) -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    pull = gh(f"repos/{repo}/pulls/{args.pr}")
    current_labels = {label["name"] for label in pull["labels"]}
    files = review_labels.list_pr_files(repo, args.pr, gh)
    current_fp = review_labels.diff_fingerprint(files)
    ai_comment = review_labels.latest_ai_comment(repo, args.pr, gh)
    stored_fp = (review_labels.header_facts(ai_comment.get("body") or "").get("diff")
                 if ai_comment else None)
    run_needed = review_labels.should_run_ai_review(current_labels, stored_fp, current_fp)
    # Единственная строка на stdout — bash-шаг ai-review.yml читает её как
    # $(...), никакого другого вывода в этой команде быть не должно.
    print("true" if run_needed else "false")
    return 0


# ── verdict: разбор ответа, комментарий, метка ────────────────────────────────

def apply_large_ok(repo: str, pr: int, added: int, current_labels, verdict: str) -> None:
    """Проводка чистого large_ok_decision: ставит review:large-ok сама, либо
    эскалирует владельцу по каналу pulse_guard.escalate (#204, п.3). Молчит
    на «skip» — дифф не review:large или AI не одобрил, ничего не меняется."""
    decision = large_ok_decision(added, current_labels, verdict)
    if decision == "skip":
        return
    if decision == "ok":
        run_gh("api", "-X", "POST", f"repos/{repo}/issues/{pr}/labels",
               "-f", f"labels[]={review_labels.LARGE_OK}")
        print(f"large-ok: +{added} строк ≤ {check_pr.LARGE_DIFF_HUGE_LINES} — "
              f"{review_labels.LARGE_OK} поставлена автоматически")
        return
    text = huge_diff_escalation_text(pr, added)
    result = pulse_guard.escalate(repo, pulse_guard.WATCHDOG_ISSUE, text)
    print(f"::warning::large-ok: +{added} строк > {check_pr.LARGE_DIFF_HUGE_LINES} — "
          f"эскалация владельцу ({result})")


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

    # Газ к тормозу review:large (#204): подтверждение размера опирается на
    # состоявшийся вердикт AI, а не на факт запуска — считается ПОСЛЕ того,
    # как ai:*-метка на месте, чтобы large_ok_decision видел актуальный verdict.
    files = review_labels.list_pr_files(repo, args.pr, gh)
    added = sum(f["additions"] for f in files)
    apply_large_ok(repo, args.pr, added, current | {label}, verdict)

    # Отпечаток диффа (#252) — в шапку комментария, чтобы check_pr.py на
    # следующем пуше мог сравнить и сохранить метку, если PR не изменился
    # (см. review_labels.diff_fingerprint/diff_unchanged).
    diff_fp = review_labels.diff_fingerprint(files)
    body = build_comment(args.pr, args.head, verdict, findings, tasks, diff_fp=diff_fp)
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

    should_run = sub.add_parser(
        "should-run", help="нужен ли дорогой прогон (печатает true/false, #294)")
    should_run.add_argument("--pr", type=int, required=True)
    should_run.set_defaults(func=cmd_should_run)

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
