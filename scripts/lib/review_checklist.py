#!/usr/bin/env python3
"""Третья категория находок ревью (#462): некритичное замечание при `approve`.

Контракт вердикта уже знал два исхода: блокирующая находка → rework (проза
до 5 замечаний, gate `ai:changes-requested`); полезная работа вне рамок PR →
блок ЗАДАЧА → issue (`scripts/review/file_tasks.py`). Замечание при `approve`
падало между ними: ревьюер вправе одобрить PR прозой «мелкое, не блокирует»
— эта проза не блокер и не задача, PR сливается, замечание испаряется
(живой замер #462: 5 таких находок в 3 из 29 просмотренных approve-вердиктов
за 2026-09-06 — #452, #285, #180).

Третий исход: блок ЗАМЕЧАНИЕ: <заголовок> / тело / КОНЕЦ ЗАМЕЧАНИЯ (парсинг
— parse_remarks здесь, тот же стиль, что ЗАДАЧА/КОНЕЦ ЗАДАЧИ в
scripts/review/ai_review.py) — только при вердикте `approve` (см.
ai_prompt.md). Не превращается в issue немедленно: сливается в чеклист
ЖИВЁТ В ТЕЛЕ PR (не в комментарии — тело переживает прокрутку и видно сразу,
не изобретаем второе хранилище). Автор отмечает пункт нативным GitHub
чекбоксом (`- [ ]` → `- [x]` в теле issue/PR — редактирует тело кликом,
без стороннего кода) — этот файл только читает/пишет тот же текст.

Слияние с незакрытыми пунктами не блокируется (перевело бы некритичное в
критичное и вернуло бы конвейер к вечным кругам, тот же класс, что и решение
про `review:large`) и не теряется молча: after_merge в scheduler.py читает
unresolved_items() и заводит ОДНУ задачу-хвост со ссылкой на PR (не тучу —
именно поэтому решение не «issue на каждый пункт»).

Оценка ценности/приоритета находок в чеклисте — решение человека, машины
здесь нет (см. proposal.md #462): задача-хвост наследует только область (PR,
на который ссылается) и метку task; приоритет по графу блокировок — отдельный
незавершённый механизм (#367), сюда не привязан.
"""

import re

# Блок ЗАМЕЧАНИЕ — тот же стиль парсинга, что ЗАДАЧА/КОНЕЦ ЗАДАЧИ
# (scripts/review/ai_review.py: TASK_OPEN_RE/TASK_CLOSE), сюда не
# скопирован ВТОРОЙ раз — эти два блока живут в разных модулях (ai_review.py
# разбирает формат ответа модели, здесь — общее для verdict/merge), поэтому
# отдельные константы, а не импорт: ai_review.py импортирует review_labels
# по тому же паттерну importlib, review_checklist читается симметрично
# оттуда, не наоборот (нет цикла).
REMARK_OPEN_RE = re.compile(r"^ЗАМЕЧАНИЕ:\s*(\S.*)$")
REMARK_CLOSE = "КОНЕЦ ЗАМЕЧАНИЯ"

# Маркеры секции чеклиста в теле PR — HTML-комментарий, невидим в рендере
# GitHub, но однозначно находится regex'ом при следующем раунде ревью и при
# слиянии. Разные строки begin/end (не общий маркер с направлением) — чтобы
# оператор split() не путал начало предыдущей секции с концом текущей на
# теле, где секция случайно встретилась дважды (защита от искажённого тела).
CHECKLIST_BEGIN = "<!-- ai-review:checklist:begin -->"
CHECKLIST_END = "<!-- ai-review:checklist:end -->"
CHECKLIST_TITLE = "### Замечания ревью (не блокируют слияние)"

# Пункт чеклиста — нативный GitHub task-list-item. `[ ]`/`[x]`/`[X]` — три
# формы, которые реально пишет GitHub при клике (заглавная X на некоторых
# путях API); регистр итогового чекбокса не имеет значения для нас, только
# сам факт «отмечен».
#
# Заголовок — В ЖИРНОМ ШРИФТЕ (`**title**`), не голым текстом до тире:
# заголовок находки САМ вправе содержать « — » (пример из практики: «файл —
# устаревшее число»), и разбор по первому тире молча схлопнул бы заголовок
# с описанием на дедупликации следующего раунда (находка теста
# test_merge_checklist_dedupes_by_exact_title_keeps_existing_state — заголовок
# без bold-обрамления не восстанавливается однозначно). `\*\*` экранирован —
# литеральные звёздочки Markdown, не quantifier.
_ITEM_RE = re.compile(r"^- \[( |x|X)\]\s*\*\*(.+?)\*\*(?:\s+—\s+(.*))?$")


def parse_remarks(answer: str) -> list[dict]:
    """Блоки ЗАМЕЧАНИЕ: … КОНЕЦ ЗАМЕЧАНИЯ из ответа модели. Незакрытый блок
    отбрасывается целиком — тот же принцип, что parse_tasks в ai_review.py:
    половина находки в чеклисте хуже отсутствия находки."""
    remarks: list[dict] = []
    title: str | None = None
    body: list[str] = []
    for line in (answer or "").splitlines():
        stripped = line.strip()
        if title is None:
            match = REMARK_OPEN_RE.match(stripped)
            if match:
                title = match.group(1).strip()
                body = []
        elif stripped == REMARK_CLOSE:
            remarks.append({"title": title, "body": "\n".join(body).strip()})
            title, body = None, []
        else:
            body.append(line.rstrip())
    return [r for r in remarks if r["title"]]


def _read_section(pr_body: str) -> tuple[str, list[tuple[bool, str, str]], str]:
    """(текст до секции, [(отмечен, заголовок, детали)], текст после секции).
    Секции нет вовсе — всё тело уходит в «до», пунктов нет, «после» пусто."""
    pr_body = pr_body or ""
    if CHECKLIST_BEGIN not in pr_body or CHECKLIST_END not in pr_body:
        return pr_body, [], ""
    before, rest = pr_body.split(CHECKLIST_BEGIN, 1)
    section, after = rest.split(CHECKLIST_END, 1)
    items: list[tuple[bool, str, str]] = []
    for line in section.splitlines():
        match = _ITEM_RE.match(line.strip())
        if match:
            items.append((match.group(1).lower() == "x", match.group(2).strip(),
                          (match.group(3) or "").strip()))
    return before.rstrip("\n"), items, after.lstrip("\n")


def render_section(items: list[tuple[bool, str, str]]) -> str:
    lines = [CHECKLIST_BEGIN, CHECKLIST_TITLE, ""]
    for checked, title, detail in items:
        mark = "x" if checked else " "
        tail = f" — {detail}" if detail else ""
        lines.append(f"- [{mark}] **{title}**{tail}")
    lines.append(CHECKLIST_END)
    return "\n".join(lines)


def merge_checklist(pr_body: str, remarks: list[dict]) -> str | None:
    """Новое тело PR с добавленными пунктами; None — писать нечего (нет ни
    новых замечаний, ни существующей секции — PATCH не нужен).

    Уже существующие пункты НЕ трогаются: отмеченные автором чекбоксы
    (checked=True) переживают повторный раунд ревью без затирания — новые
    замечания только ДОПИСЫВАЮТСЯ. Дедупликация — по точному заголовку
    (не по отрендеренной строке целиком — заголовок сам вправе содержать
    « — », см. _ITEM_RE): повторная находка с тем же заголовком в следующем
    раунде не плодит вторую строку (тот же принцип, что open_task_titles в
    file_tasks.py)."""
    before, items, after = _read_section(pr_body)
    existing_titles = {title for _, title, _ in items}
    added = False
    for remark in remarks:
        title = remark["title"].strip()
        if not title or title in existing_titles:
            continue
        detail = remark["body"].splitlines()[0].strip() if remark["body"].strip() else ""
        items.append((False, title, detail))
        existing_titles.add(title)
        added = True
    if not added:
        return None  # ни одного нового пункта — существующая секция (если есть) не меняется
    section = render_section(items)
    parts = [p for p in (before.strip(), section, after.strip()) if p]
    return "\n\n".join(parts) + "\n"


def unresolved_items(pr_body: str) -> list[str]:
    """Незакрытые (не отмеченные) пункты чеклиста в теле PR — на момент
    слияния решают судьбу «одна задача-хвост или ничего» (see after_merge).
    Текст пункта — заголовок (+ « — детали», если были) одной строкой, для
    прямого использования в теле задачи-хвоста."""
    _, items, _ = _read_section(pr_body)
    return [f"{title} — {detail}" if detail else title
            for checked, title, detail in items if not checked]


def tail_issue_title(pr: int) -> str:
    """Заголовок задачи-хвоста — один на PR (идемпотентность по заголовку,
    тот же приём, что file_tasks.py: повторный прогон after_merge на уже
    заведённом хвосте не дублирует issue)."""
    return f"Хвост чеклиста ревью PR #{pr}"


def tail_issue_body(repo: str, pr: int, unresolved: list[str]) -> str:
    """Тело задачи-хвоста: ссылка на PR + все незакрытые пункты одним
    списком (не по issue на пункт — «одна задача-хвост, не туча», решение
    proposal.md #462). Область и приоритет — не оценка машины (см. модульный
    docstring): задача наследует только то, что видно по построению —
    ссылку на породивший PR; ценность и приоритет решает человек при разборе
    пула, как и для остальных задач без формального приоритета (#367 не
    смержен, пул сейчас по возрасту)."""
    lines = [
        f"PR #{pr} (https://github.com/{repo}/pull/{pr}) слит с незакрытыми "
        "пунктами чеклиста ревью — не блокировали слияние (третья категория "
        "находок: некритично для мержа, но сделать надо), одна задача на все "
        "незакрытые пункты, не issue на каждый.",
        "",
    ]
    lines += [f"- [ ] {text}" for text in unresolved]
    lines += [
        "",
        "Область и приоритет — как у породившего PR; отдельной оценки "
        "ценности эта задача не несёт, решает тот, кто берёт её из пула.",
    ]
    return "\n".join(lines)
