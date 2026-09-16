#!/usr/bin/env python3
"""Реестр незакрытых находок ревью, ключ — затронутый файл (#1262, объединяет
#1217: находка ревью обязана пережить как следующий раунд ревью ТОГО ЖЕ PR,
так и слияние — сегодня переживает ни то, ни другое).

До этого файла третья категория находок (#462, ЗАМЕЧАНИЕ, см.
review_checklist.py) при слиянии с незакрытыми пунктами заводила ОДНУ задачу
пула на PR («Хвост чеклиста ревью PR #N», scheduler.py::after_merge). Замер
2026-09-14 (#1262): 110 таких задач открыты, 0 когда-либо взято в работу
(0 назначенных, 0 веток agent/<хвост>-*, 85/110 несут stale-unclaimed) — приток
пула ~19/сутки, ~27% всего пула, закрытие ровно ноль. Тайбрейк приоритета
пула — номер issue по возрастанию (free_task.py::issue_priority_key): хвост
по построению всегда самая свежая issue, то есть всегда в конце своего тира;
дать ему приоритет уже пробовали (checklist_tail_labels.py, #1178) — очередь
забилась хвостами, барьерные задачи ушли в конец, потребовался НОВЫЙ тир
приоритета (#1179) специально чтобы обойти хвосты. Дефект был в носителе, не
в приоритете: находка ценой в одну строку комментария стоила аренды, ветки,
PR и прогона воркера — самого дефицитного ресурса конвейера.

Носитель здесь — файл `findings.json` на выделенной ветке данных
`data/review-findings` (тот же класс решения, что уже несёт
`data_branch_writer.py`/`data/pipeline-health`, #882: телеметрия и
накопленное состояние не смешиваются с main, где каждая правка требует PR).
Отличие от data_branch_writer: писатель здесь ровно один (scheduler.py в
workflow orchestra, concurrency-группа "orchestra" уже сериализует ВСЕ его
прогоны, см. orchestra.yml) — второй параллельный писатель физически не
существует, поэтому retry-на-гонку (git push race) здесь не заводится:
это был бы код, доказывающий отсутствующую опасность. Запись — через GitHub
Contents API (GET текущего blob + PUT нового содержимого), не git clone:
файл маленький (JSON на несколько сотен записей), а Contents API уже
используется этим же процессом для похожей проверки (scheduler.py::
docs_missing читает repos/{repo}/contents/{file}?ref=main тем же способом).

Права: запись требует `contents: write` — есть у workflow orchestra (весь
файл, permissions в шапке orchestra.yml), НЕТ у job'а ai-review verdict
(contents: read, #939 явно снял issues: write и не возвращает вовсе). Поэтому
запись живёт ТОЛЬКО в scheduler.py::after_merge (при слиянии PR), не в
ai_review.py::cmd_verdict — тот только читает реестр (cmd_gather, contents:
read достаточно для GET) и разбирает НАХОДКА-ЗАКРЫТА из ответа модели в
МАШИННЫЙ МАРКЕР ТЕЛА PR (`merge_resolved_marker`, HTML-комментарий
`<!-- ai-review:resolved-findings:… -->`, отдельный PATCH тела) — сам реестр
не трогает. after_merge на слиянии читает и закрытые id из маркера ТЕЛА
(`parse_resolved_marker(pull["body"])`, без сети — тело уже в руках), и
незакрытые пункты чеклиста PR (review_checklist.unresolved_findings) — это
ЕДИНСТВЕННЫЙ момент мутации. Носителем факта закрытия НЕ является шапка
комментария: тот носитель отвергнут design.md (развилка 3) — он требовал бы
лишний GET истории комментариев на каждом слиянии.

Итог: то же самое ai-review, что уже гоняется на каждый PR, получает выписку
находок по трогаемым PR файлам (ai_review.py::findings_section) и либо видит
находку исправленной (блок НАХОДКА-ЗАКРЫТА), либо не трогает её — она
остаётся в реестре и всплывёт на следующем PR, тронувшем тот же файл. Файл,
который не трогают годами, честно хранит находку вечно — механизма
принудительного возврата к нему в этой правке нет (см. proposal.md #1262,
раздел «Что происходит с находкой в нетронутом файле»).
"""

from __future__ import annotations

import base64
import json
import re
from typing import Callable

GhFn = Callable[..., dict | list | None]

# Ветка данных — не main (правило репозитория «прямой пуш в main не проходит
# обязательные проверки», AGENTS.md): реестр не носит поведения кода, только
# накопленное состояние, тот же класс, что data/pipeline-health.
REGISTRY_BRANCH = "data/review-findings"
REGISTRY_PATH = "findings.json"


def empty_registry() -> dict:
    return {"next_id": 1, "findings": []}


def load_registry(text: str | None) -> dict:
    """Пустой/отсутствующий текст — пустой реестр (ветка/файл ещё не
    существуют — первый писатель создаёт их, см. sync_after_merge), не
    ошибка: тот же приём, что review_checklist._read_section для тела без
    секции чеклиста.

    Битый JSON/JSON не-объект — RuntimeError с причиной, НЕ голый
    json.JSONDecodeError (наследник ValueError): оба потребителя реестра
    (ai_review.py::findings_section, scheduler.py::after_merge) ловят
    RuntimeError — дельта-спека этого PR требует дословно «сбой чтения
    реестра (сеть, битый JSON) не роняет gather», а неспецошибка пролетала
    бы сквозь оба обработчика и роняла бы cmd_gather целиком и пульс
    оркестратора ПОСЛЕ уже состоявшегося мержа (находка ревью PR #1268)."""
    if not text or not text.strip():
        return empty_registry()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"реестр находок: битый JSON ({error}) — чтение не состоялось, "
            "это не «находок нет»") from error
    if not isinstance(data, dict):
        raise RuntimeError(
            f"реестр находок: JSON не объект ({type(data).__name__}) — "
            "чтение не состоялось, это не «находок нет»")
    data.setdefault("next_id", 1)
    data.setdefault("findings", [])
    return data


def dump_registry(registry: dict) -> str:
    return json.dumps(registry, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def add_finding(registry: dict, file: str, title: str, detail: str,
                 source_pr: int, opened_at: str) -> int:
    """Возвращает id новой находки (последовательный счётчик реестра, не
    UUID — id должен быть коротким и произносимым моделью в блоке
    НАХОДКА-ЗАКРЫТА: <id>)."""
    finding_id = registry["next_id"]
    registry["next_id"] = finding_id + 1
    registry["findings"].append({
        "id": finding_id,
        "file": file,
        "title": title,
        "detail": detail,
        "source_pr": source_pr,
        "opened_at": opened_at,
        "status": "open",
    })
    return finding_id


def close_findings(registry: dict, ids: list[int], closed_by_pr: int) -> list[int]:
    """Возвращает реально закрытые id. Отсутствующий/уже закрытый/чужой id —
    не ошибка (модель могла сослаться на устаревший номер, увиденный в
    прошлом раунде выписки) — тот же принцип терпимости, что _read_section
    в review_checklist к строкам вне формы пункта: тихий пропуск точнее,
    чем громкий отказ вердикта из-за одной неверной цифры."""
    open_ids = {f["id"] for f in registry["findings"] if f.get("status") == "open"}
    closed = [i for i in ids if i in open_ids]
    closed_set = set(closed)
    for finding in registry["findings"]:
        if finding["id"] in closed_set:
            finding["status"] = "closed"
            finding["closed_by_pr"] = closed_by_pr
    return closed


def open_findings_for_files(registry: dict, files: list[str]) -> list[dict]:
    fileset = set(files)
    return [f for f in registry.get("findings", [])
            if f.get("status") == "open" and f.get("file") in fileset]


def render_findings_section(findings: list[dict]) -> str:
    """Выписка находок по файлам этого PR — $findings_section в
    ai_prompt.md. Пусто — явная строка «находок нет», не пустая секция:
    отсутствие текста неотличимо от пропавшего плейсхолдера, «находок нет»
    неотличимо только от того, что и означает (симметрично defect_classes.
    render_prompt_section_unavailable ниже — там же объяснён этот приём)."""
    if not findings:
        return "Открытых находок реестра по файлам этого PR нет."
    lines = [
        "Открытые находки реестра (заведены через #1262) по файлам этого PR — "
        "если код уже чинит находку, закрой её строкой `НАХОДКА-ЗАКРЫТА: <id>` "
        "(можно несколько строк); не упомянутая находка останется открытой "
        "сама, повторно заводить её как ЗАМЕЧАНИЕ не нужно:",
        "",
    ]
    by_file: dict[str, list[dict]] = {}
    for finding in findings:
        by_file.setdefault(finding["file"], []).append(finding)
    for file in sorted(by_file):
        lines.append(f"### {file}")
        for finding in sorted(by_file[file], key=lambda item: item["id"]):
            detail = f" — {finding['detail']}" if finding.get("detail") else ""
            lines.append(
                f"- [{finding['id']}] **{finding['title']}**{detail} "
                f"(источник PR #{finding['source_pr']})")
        lines.append("")
    return "\n".join(lines).rstrip()


def render_unavailable(reason: str) -> str:
    """Реестр недоступен (сеть/права/битый JSON) — назвать причину, не
    молчать «находок нет» вместо честного «не прочитано» (AGENTS.md,
    «Алерт не гадает» — то же различение, что уже применяет
    defect_classes_section к своему источнику)."""
    return (f"Реестр находок недоступен ({reason}) — выписка по файлам этого "
            "PR пропущена; это НЕ означает, что открытых находок нет.")


# ── Чтение (job review/gather, contents: read достаточно) ────────────────────

def _is_not_found(error: Exception) -> bool:
    """Та же точная форма, что ai_review.is_not_found/scheduler.docs_missing
    (`"HTTP 404" in str(error)`, не по подстроке «404» — она ловит и URL) —
    третья независимая копия этого приёма в репозитории, не первая; общий
    хелпер здесь не заводится, потому что каждый вызывающий несёт свой gh()
    поверх `gh api` (см. pool_issue.py docstring — то же решение, тот же
    класс "инъекция транспорта, не импорт")."""
    return "HTTP 404" in str(error)


def fetch_registry(gh_func: GhFn, repo: str) -> tuple[dict, str | None]:
    """(реестр, sha текущего blob'а). sha=None — файла/ветки ещё нет
    (первая находка когда-нибудь создаст и то, и другое, см.
    sync_after_merge._ensure_branch). Любой отказ, кроме точного «файла нет»,
    поднимается наверх — вызывающий (findings_section) обязан отличить
    «находок нет» от «не прочитано» (см. render_unavailable).

    Ответ `encoding: "none"` с пустым content — отказ, не пустой реестр:
    GET /contents/{path} отдаёт такую форму для файла КРУПНЕЕ потолка 1 МБ
    (HTTP 200, content="", encoding="none"). Трактовать её как пустой
    реестр значило бы два silent-wrong разом: выписка findings_section
    отвечала бы «находок нет» при полном реестре, а sync_after_merge
    приписал бы новые пункты к ПУСТОМУ реестру и записал бы его с валидным
    sha — молчаливо стерев все накопленные находки (находка ревью
    PR #1268; файл растёт монотонно — closed записи не удаляются, — так
    что потолок не теоретический). RuntimeError с причиной: оба
    потребителя уже умеют громко деградировать (render_unavailable /
    ⚠️-observation), а запись при этом не состоится."""
    try:
        payload = gh_func(f"repos/{repo}/contents/{REGISTRY_PATH}?ref={REGISTRY_BRANCH}")
    except RuntimeError as error:
        if _is_not_found(error):
            return empty_registry(), None
        raise
    if payload.get("encoding") == "none" or (payload.get("sha") and not payload.get("content")):
        raise RuntimeError(
            f"реестр находок: файл крупнее потолка Contents API 1 МБ "
            f"(encoding={payload.get('encoding')!r}, content пуст) — чтение "
            "не состоялось, это не «находок нет»; записывать поверх нельзя, "
            "иначе реестр будет стёрт (нужна компактация реестра, см. "
            "proposal.md)")
    content = base64.b64decode(payload["content"]).decode("utf-8")
    return load_registry(content), payload["sha"]


# ── Запись (только scheduler.py::after_merge и разовая миграция #1262,
# оба — contents: write) ──────────────────────────────────────────────────

def _ensure_branch(gh_func: GhFn, repo: str) -> None:
    ref = f"heads/{REGISTRY_BRANCH}"
    try:
        gh_func(f"repos/{repo}/git/ref/{ref}")
        return
    except RuntimeError as error:
        if not _is_not_found(error):
            raise
    # Ветки данных ещё нет вовсе (первая находка в истории репозитория) —
    # заводим от текущего main, тот же выбор базы, что data_branch_writer.
    # clone_data_branch делает для data/pipeline-health.
    main_ref = gh_func(f"repos/{repo}/git/ref/heads/main")
    sha = main_ref["object"]["sha"]
    gh_func("-X", "POST", f"repos/{repo}/git/refs",
            "-f", f"ref=refs/heads/{REGISTRY_BRANCH}", "-f", f"sha={sha}")


def write_registry(gh_func: GhFn, repo: str, registry: dict, sha: str | None,
                    message: str) -> None:
    """PUT нового содержимого реестра — общий хвост sync_after_merge и
    разовой миграции (scripts/lib/migrate_review_findings.py::cmd_apply),
    не две копии одного и того же PUT+ensure_branch."""
    if sha is None:
        _ensure_branch(gh_func, repo)
    encoded = base64.b64encode(dump_registry(registry).encode("utf-8")).decode("ascii")
    args = ["-X", "PUT", f"repos/{repo}/contents/{REGISTRY_PATH}",
            "-f", f"message={message}",
            "-f", f"content={encoded}", "-f", f"branch={REGISTRY_BRANCH}"]
    if sha:
        args += ["-f", f"sha={sha}"]
    gh_func(*args)


def sync_after_merge(gh_func: GhFn, repo: str, pr_number: int,
                      new_items: list[dict], resolved_ids: list[int],
                      opened_at: str) -> dict:
    """Единственная точка мутации реестра (см. докстринг модуля — почему
    именно здесь, а не в ai_review.py). new_items — unresolved_findings()
    слитого PR (file/title/detail); пункты без file (ЗАМЕЧАНИЕ без
    обязательного ФАЙЛ) не ключуются реестром — остаются видны только в
    чеклисте самого PR, это НЕ потеря (правило `# половина находки хуже
    отсутствия` здесь работает в обратную сторону: без файла находку некуда
    вернуть при следующем ревью, честнее не притворяться, что реестр её
    несёт). resolved_ids — parse_resolved_marker() ТЕЛА этого PR (маркер
    `<!-- ai-review:resolved-findings:… -->`, записан cmd_verdict через
    merge_resolved_marker при вердикте; design.md развилка 3 отвергла
    носитель «шапка комментария» — он требовал бы отдельный GET истории
    комментариев на каждое слияние).

    Возвращает {"added": [id,...], "closed": [id,...], "skipped": int}
    — skipped считает пункты БЕЗ file, для видимого отчёта в actions
    after_merge (не тихая потеря).

    Сеть не трогается вовсе, если писать нечего (все пункты без file и
    закрывать нечего) — тот же принцип экономии, что у старого
    create_pool_issue (сетевой вызов только под условием `if unresolved`),
    не заводим новый безусловный GET там, где раньше не было ни одного."""
    keyed_items = [item for item in new_items if (item.get("file") or "").strip()]
    skipped = len(new_items) - len(keyed_items)
    if not keyed_items and not resolved_ids:
        return {"added": [], "closed": [], "skipped": skipped}
    registry, sha = fetch_registry(gh_func, repo)
    added_ids = [
        add_finding(registry, item["file"].strip(), item["title"], item.get("detail", ""),
                    pr_number, opened_at)
        for item in keyed_items
    ]
    closed_ids = close_findings(registry, resolved_ids, pr_number) if resolved_ids else []
    if not added_ids and not closed_ids:
        return {"added": [], "closed": [], "skipped": skipped}
    write_registry(gh_func, repo, registry, sha,
                   f"review-findings: PR #{pr_number} (+{len(added_ids)}/-{len(closed_ids)})")
    return {"added": added_ids, "closed": closed_ids, "skipped": skipped}


# ── НАХОДКА-ЗАКРЫТА: <id> — контракт ответа модели (ai_prompt.md) ────────────
#
# Одна строка на находку, тот же стиль, что КЛАСС: (defect_classes.
# CLASS_LINE_RE) — не блок-забор (ЗАМЕЧАНИЕ/ЗАДАЧА): тело не нужно, только
# ссылка на уже существующий id из выписки findings_section.
RESOLVED_LINE_RE = re.compile(r"^(``|`|\*\*|__|)НАХОДКА-ЗАКРЫТА:\s*#?(\d+)\s*\.?\1\s*$")


def parse_resolved_ids(answer: str) -> list[int]:
    """Список id находок, которые модель считает исправленными в этом
    раунде — порядок первого упоминания, без дублей (findings_of ниже
    вырезает эти строки из свободной прозы комментария тем же приёмом, что
    defect_classes.CLASS_LINE_RE).

    Допуск markdown-обрамления (``…``, `…`, **…**, __…__) — тот же приём,
    что VERDICT_RE в ai_review.py: это контрактная строка ВЕРХНЕГО уровня
    (модель велена писать её отдельной строкой), и живая практика знает её
    обрамлённой — требовать голую форму значило бы терять закрытие находки
    из-за кавычек, которые модель ставит по привычке кода (класс ловли
    «строка есть, парсер её не видит», тот же, что у КЛАСС)."""
    ids: list[int] = []
    seen: set[int] = set()
    for line in (answer or "").splitlines():
        match = RESOLVED_LINE_RE.match(line.strip())
        if match:
            finding_id = int(match.group(2))
            if finding_id not in seen:
                seen.add(finding_id)
                ids.append(finding_id)
    return ids


# ── Маркер закрытых находок в ТЕЛЕ PR — тот же выбор носителя, что чеклист
# ЗАМЕЧАНИЕ (#462): after_merge уже читает pull["body"] без отдельного
# сетевого запроса, второй источник (сканирование истории комментариев PR
# ради этого поля) не заводится — дороже (лишний GET на каждое слияние) и
# создал бы второе место правды о том же факте.
RESOLVED_MARKER_RE = re.compile(r"<!-- ai-review:resolved-findings:([0-9, ]*) -->")


def parse_resolved_marker(pr_body: str) -> list[int]:
    """Id находок, отмеченных НАХОДКА-ЗАКРЫТА хотя бы в одном раунде ревью
    этого PR — читает scheduler.py::after_merge при слиянии (без сети,
    маркер уже в pull["body"])."""
    match = RESOLVED_MARKER_RE.search(pr_body or "")
    if not match:
        return []
    return [int(token) for token in match.group(1).split(",") if token.strip().isdigit()]


def merge_resolved_marker(pr_body: str, resolved_ids: list[int]) -> str | None:
    """Новое тело PR с ОБЪЕДИНЁННЫМ (union) списком id в маркере; None —
    писать нечего (новых id нет — существующий маркер, если есть, не
    трогаем). Объединение, не перезапись — модель следующего раунда может
    не повторить id, закрытый раньше (та же терпимость, что merge_checklist:
    забыть упомянуть — не то же самое, что «передумал»)."""
    pr_body = pr_body or ""
    existing = parse_resolved_marker(pr_body)
    merged = sorted(set(existing) | set(resolved_ids))
    if merged == existing:
        return None
    marker = f"<!-- ai-review:resolved-findings:{','.join(str(i) for i in merged)} -->"
    if RESOLVED_MARKER_RE.search(pr_body):
        return RESOLVED_MARKER_RE.sub(marker, pr_body, count=1)
    return pr_body.rstrip("\n") + "\n\n" + marker + "\n"
