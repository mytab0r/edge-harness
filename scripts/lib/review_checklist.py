#!/usr/bin/env python3
"""Третья категория находок ревью (#462): некритичное замечание при `approve`.

Контракт вердикта уже знал два исхода: блокирующая находка → rework (проза
до 5 замечаний, gate `ai:changes-requested`); полезная работа вне рамок PR →
блок ЗАДАЧА → issue (`scripts/review/file_tasks.py`). Замечание при `approve`
падало между ними: ревьюер вправе одобрить PR прозой «мелкое, не блокирует»
— эта проза не блокер и не задача, PR сливается, замечание испаряется
(живой замер #462: 5 таких находок в 3 из 29 просмотренных approve-вердиктов
за 2026-09-06 — #452, #285, #180).

Третий исход: блок ЗАМЕЧАНИЕ: <заголовок> / ФАЙЛ: <путь> / тело / КОНЕЦ
ЗАМЕЧАНИЯ (парсинг — parse_remarks здесь, тот же стиль, что ЗАДАЧА/КОНЕЦ
ЗАДАЧИ в scripts/review/ai_review.py) — только при вердикте `approve` (см.
ai_prompt.md). Не превращается в issue немедленно: сливается в чеклист
ЖИВЁТ В ТЕЛЕ PR (не в комментарии — тело переживает прокрутку и видно сразу,
не изобретаем второе хранилище). Автор отмечает пункт нативным GitHub
чекбоксом (`- [ ]` → `- [x]` в теле issue/PR — редактирует тело кликом,
без стороннего кода) — этот файл только читает/пишет тот же текст.

Слияние с незакрытыми пунктами не блокируется (перевело бы некритичное в
критичное и вернуло бы конвейер к вечным кругам, тот же класс, что и решение
про `review:large`) и не теряется молча — но носитель после слияния сменился
(#1262, объединяет #1217): раньше after_merge заводил ОДНУ задачу-хвост со
ссылкой на PR (`create_pool_issue`, метка `task`); замер 2026-09-14 показал
0 закрытых из 110 заведённых — задача пула стоит аренды/ветки/PR/прогона
воркера ради находки ценой в одну строку. Теперь after_merge переносит
незакрытые пункты в файловый реестр (`review_findings.py`, ветка данных
`data/review-findings`) через `unresolved_findings()` ниже — тот же принцип
«не потерять», другой носитель, не потребляющий пул и прогоны воркера.

ФАЙЛ — обязательное второе поле блока ЗАМЕЧАНИЕ (тот же приём, что МАСШТАБ у
блока ЗАДАЧА в ai_review.py): без него находка НЕ ключуется реестром при
слиянии (реестр не умеет вернуть находку без адреса — некуда её вернуть при
следующем ревью), но по-прежнему живёт в чеклисте ЭТОГО PR — половина
потери лучше полной (тот же класс терпимости, что у незакрытого блока
целиком, см. parse_remarks).

Оценка ценности/приоритета находок в чеклисте — решение человека, машины
здесь нет (см. proposal.md #462): приоритет по графу блокировок — отдельный
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

# ФАЙЛ — обязательная ВТОРАЯ строка блока (сразу после заголовка), тот же
# приём, что МАСШТАБ у ЗАДАЧА в ai_review.py. Отсутствие строки — не
# ошибка парсинга (см. parse_remarks: file остаётся None, тело начинается с
# этой же строки) — просто находка не ключуется реестром при слиянии
# (review_findings.sync_after_merge пропускает пункты без file).
REMARK_FILE_RE = re.compile(r"^ФАЙЛ:\s*(\S.*)$")

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
#
# Файл — опциональный третий сегмент в обратных кавычках СРАЗУ после
# заголовка (`` `путь/к/файлу` ``), ДО « — детали»: код (не проза) внутри
# markdown code span читается однозначно, « — » внутри самого пути файлов
# репозитория не встречается (замерено: ни один путь в дереве не содержит
# такую последовательность). Пункты, слитые ДО этой правки (#1262), файла не
# несут — регэксп принимает его отсутствие (обратная совместимость с уже
# открытыми PR, чей чеклист начат старым форматом).
_ITEM_RE = re.compile(r"^- \[( |x|X)\]\s*\*\*(.+?)\*\*(?:\s+`([^`]+)`)?(?:\s+—\s+(.*))?$")


def parse_remarks(answer: str) -> list[dict]:
    """Блоки ЗАМЕЧАНИЕ: … КОНЕЦ ЗАМЕЧАНИЯ из ответа модели. Незакрытый блок
    отбрасывается целиком — тот же принцип, что parse_tasks в ai_review.py:
    половина находки в чеклисте хуже отсутствия находки.

    Строка ФАЙЛ: <путь> сразу после заголовка (если есть) уходит в поле
    `file`, а не в тело — тот же приём, что МАСШТАБ у блока ЗАДАЧА
    (partition_tasks в ai_review.py читает второе поле отдельно от тела).
    Если второй строки нет или она не в форме ФАЙЛ: — `file` остаётся None,
    и всё, что дальше, включая эту строку, уходит в тело как раньше
    (обратная совместимость контракта, см. docstring модуля)."""
    remarks: list[dict] = []
    title: str | None = None
    file: str | None = None
    expect_file: bool = False
    body: list[str] = []
    for line in (answer or "").splitlines():
        stripped = line.strip()
        if title is None:
            match = REMARK_OPEN_RE.match(stripped)
            if match:
                title = match.group(1).strip()
                file = None
                expect_file = True
                body = []
            continue
        if expect_file:
            expect_file = False
            fmatch = REMARK_FILE_RE.match(stripped)
            if fmatch:
                file = fmatch.group(1).strip()
                continue
            # Строки ФАЙЛ нет — эта строка уже часть тела, не теряем её.
        if stripped == REMARK_CLOSE:
            remarks.append({"title": title, "file": file, "body": "\n".join(body).strip()})
            title, file, body = None, None, []
        else:
            body.append(line.rstrip())
    return [r for r in remarks if r["title"]]


def _read_section(pr_body: str) -> tuple[str, list[tuple[bool, str, str | None, str]], str]:
    """(текст до секции, [(отмечен, заголовок, файл|None, детали)], текст
    после секции). Секции нет вовсе — всё тело уходит в «до», пунктов нет,
    «после» пусто."""
    pr_body = pr_body or ""
    if CHECKLIST_BEGIN not in pr_body or CHECKLIST_END not in pr_body:
        return pr_body, [], ""
    before, rest = pr_body.split(CHECKLIST_BEGIN, 1)
    section, after = rest.split(CHECKLIST_END, 1)
    items: list[tuple[bool, str, str | None, str]] = []
    for line in section.splitlines():
        match = _ITEM_RE.match(line.strip())
        if match:
            items.append((
                match.group(1).lower() == "x",
                match.group(2).strip(),
                (match.group(3) or "").strip() or None,
                (match.group(4) or "").strip(),
            ))
    return before.rstrip("\n"), items, after.lstrip("\n")


def render_section(items: list[tuple[bool, str, str | None, str]]) -> str:
    lines = [CHECKLIST_BEGIN, CHECKLIST_TITLE, ""]
    for checked, title, file, detail in items:
        mark = "x" if checked else " "
        file_tag = f" `{file}`" if file else ""
        tail = f" — {detail}" if detail else ""
        lines.append(f"- [{mark}] **{title}**{file_tag}{tail}")
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
    existing_titles = {title for _, title, _, _ in items}
    added = False
    for remark in remarks:
        title = remark["title"].strip()
        if not title or title in existing_titles:
            continue
        detail = remark["body"].splitlines()[0].strip() if remark["body"].strip() else ""
        items.append((False, title, remark.get("file"), detail))
        existing_titles.add(title)
        added = True
    if not added:
        return None  # ни одного нового пункта — существующая секция (если есть) не меняется
    section = render_section(items)
    parts = [p for p in (before.strip(), section, after.strip()) if p]
    return "\n\n".join(parts) + "\n"


def unresolved_findings(pr_body: str) -> list[dict]:
    """Незакрытые пункты чеклиста в структурной форме {title, file, detail}
    — вход review_findings.sync_after_merge (#1262): file=None у пунктов,
    заведённых ДО этой правки или без объявленного ФАЙЛ (см. докстринг
    модуля) — sync_after_merge пропускает их при переносе в реестр, считая
    и показывая в отчёте (не тихая потеря, см. review_findings.
    sync_after_merge докстринг про `skipped`).

    Человекочитаемой копии этого списка больше нет (`unresolved_items`
    удалён как мёртвый код, находка ревью PR #1268): единственный бывший
    вызывающий — отчёт actions after_merge — с #1262 читает структурную
    форму ниже (реестру нужен файл, не только текст), а второй формы одного
    и того же списка здесь не держим (AGENTS.md, «одно место правды»)."""
    _, items, _ = _read_section(pr_body)
    return [{"title": title, "file": file, "detail": detail}
            for checked, title, file, detail in items if not checked]
