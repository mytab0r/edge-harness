#!/usr/bin/env python3
"""Гвардия класса «не-list ответ страницы трактуется как честная пустая
страница» (дефект A, watchdog-issue #120, 2026-09-11).

Класс: `if not isinstance(chunk, list) or not chunk: break` внутри цикла
пагинации не отличает ДВА разных факта — «страниц больше нет» (`chunk == []`,
валидный список, законная короткая последняя страница) и «ответ непонятен»
(`chunk` — dict с телом ошибки вторичного рейт-лимита GitHub, `None` от
пустого тела, любая другая не-list форма). Оба варианта тихо обрывают обход
и возвращают уже накопленное (возможно нулевое) — вызывающий код читает
пустой/неполный список как «элементов нет», хотя на сервере их могло быть
сколько угодно.

Живой случай: `scheduler.open_pulls` (через `review_labels.list_pages`)
отдал `[]` при 27 реально открытых PR, ждущих доработки — `wip_gate` считал
это как «доработки нет» и открывал диспатч новых задач на ложном нуле
(`⏸️ ... 25 ≥ лимита 12` в 10:02:44Z → `✅ ... 0 < 12` в 10:25:27Z, issue
#120). Тот же класс был задублирован ЕЩЁ ТРИЖДЫ (найдено при починке этой же
задачи): `pulse_guard.all_issue_comments`/`open_ci_failure_issues`/
`ci_failure_created_since`, `contract_check._all_open_pulls` — все четыре
несли собственную копию цикла с тем же силент-дефектом, не только
`review_labels.list_pages`, где его нашли и починили первым. Все пять мест
теперь сведены в одно (`review_labels.list_pages`, который на не-list чанке
поднимает RuntimeError, а не молча обрывает обход) — вторая копия узора не
заводится.

Признак — структурный (через `ast`, не текстовый grep): `if` с условием
ровно `not isinstance(X, list) or not X` (одна и та же переменная X в обеих
половинках), в чьём теле есть `break` (while-цикл) ИЛИ `return`
(генератор с `yield from`, как file_tasks._pages — обрывает накопление
страниц так же тихо). Разбор AST, а не regex по тексту, специально:
докстринги и комментарии этого же файла и test_review_labels.py дословно
ЦИТИРУЮТ снятый узор как иллюстрацию — текстовый grep ловил бы собственную
документацию класса как нарушение (живая находка при первой версии этой
гвардии).

ЧЕСТНАЯ ГРАНИЦА (замечание ревью PR #950): гвардия ловит РОВНО эту составную
форму — узор пагинации, которым реально была задублирована ошибка во всех
пяти найденных местах (все читали чанк страницы и решали одновременно и
«это не list», и «список пуст»). Голая `if not isinstance(X, list): break`
(без второй половины) — структурно другое условие, гвардия её не матчит;
живой зонд (2026-09-11) нашёл 5 таких голых форм вне класса пагинации
(`scripts/lib/verify_transcript.py`, `scripts/measure/provider_latency.py`,
`scripts/measure/provider_model_discovery.py` ×3) — ни одна не тот же
дефект: verify_transcript падает громко (`::error::` + exit 2), остальные
две — документированный контракт «нет данных → пустой список» на разборе
чужого JSON, не тихий обрыв обхода страниц GitHub API. Расширение признака
до любого `not isinstance(X, list)` матчило бы и их — ложные срабатывания
вне мандата этой правки, не сам класс. Если появится ШЕСТОЙ инстанс именно
пагинационного узора без второй половины условия — расширять признак тогда,
на конкретном найденном случае, не заранее.

ALLOWLIST_KNOWN_INSTANCES — единственное место, где НЕ починенный инстанс
класса назван явно, а не забыт: `scripts/review/file_tasks.py` вне мандата
этой правки (параллельная работа по scripts/review/*, см. отчёт задачи) —
исключение снимается той же правкой, что чинит сам файл, не тихо.

Запуск: python -m pytest scripts/lib/test_silent_empty_page_guard.py -q
"""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# (относительный путь от корня репозитория) -> причина, по которой инстанс
# ЕЩЁ не починен этой правкой (не забыт — назван).
ALLOWLIST_KNOWN_INSTANCES = {
    "scripts/review/file_tasks.py":
        "тот же класс, что и остальные четыре (все уже сведены в "
        "review_labels.list_pages, дефект A watchdog-issue #120) — не "
        "тронут этой правкой намеренно: scripts/review/* в это же время "
        "правит параллельная работа (размерный гейт check_pr.py/"
        "ai_review.py), трогать чужой файл значило бы гарантированный "
        "конфликт слияния. Снимается той же правкой, что чинит сам файл, "
        "не тихим расширением allowlist.",
}


def _production_scripts() -> list[Path]:
    """Все .py в scripts/, кроме тестов — тот же приём, что
    test_pagination_guard.py::_production_scripts."""
    return [
        path for path in (REPO_ROOT / "scripts").rglob("*.py")
        if not path.name.startswith("test_")
    ]


def _is_isinstance_list_call(node: ast.expr, varname: str) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name) and node.func.id == "isinstance"
        and len(node.args) == 2
        and isinstance(node.args[0], ast.Name) and node.args[0].id == varname
        and isinstance(node.args[1], ast.Name) and node.args[1].id == "list"
    )


def _is_not_var(node: ast.expr, varname: str) -> bool:
    return (
        isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not)
        and isinstance(node.operand, ast.Name) and node.operand.id == varname
    )


def _stops_the_loop(body: list[ast.stmt]) -> bool:
    """True — тело `if` обрывает обход (`break` в while-цикле, `return` в
    генераторе с `yield from`, как file_tasks._pages — оба останавливают
    накопление страниц так же тихо)."""
    return any(isinstance(stmt, (ast.Break, ast.Return)) for stmt in body)


def _matched_varname(test: ast.expr) -> str | None:
    """Возвращает имя переменной, если `test` — ровно
    `not isinstance(X, list) or not X` (обе половинки об одной и той же X),
    иначе None."""
    if not (isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or) and len(test.values) == 2):
        return None
    left, right = test.values
    if not (isinstance(left, ast.UnaryOp) and isinstance(left.op, ast.Not)):
        return None
    inner = left.operand
    if not (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)
            and inner.func.id == "isinstance" and len(inner.args) == 2
            and isinstance(inner.args[0], ast.Name)
            and isinstance(inner.args[1], ast.Name) and inner.args[1].id == "list"):
        return None
    varname = inner.args[0].id
    if not _is_not_var(right, varname):
        return None
    return varname


def _find_offenders() -> list[str]:
    offenders = []
    for path in _production_scripts():
        rel = str(path.relative_to(REPO_ROOT)).replace("\\", "/")
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue  # не наш класс — не .py целиком парсибельный файл не наша забота здесь
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            varname = _matched_varname(node.test)
            if varname is None:
                continue
            if not _stops_the_loop(node.body):
                continue  # тот же тест на другую переменную, но не стоп-условие обхода
            if rel in ALLOWLIST_KNOWN_INSTANCES:
                continue
            offenders.append(f"{rel}:{node.lineno} — if not isinstance({varname}, list) or not {varname}: break/return")
    return offenders


def test_no_silent_empty_page_pattern_outside_allowlist():
    offenders = _find_offenders()
    assert offenders == [], (
        "Не-list ответ страницы (dict/None — ошибка/пустое тело) трактуется "
        "как честная короткая страница (класс #120A): "
        f"{offenders}. Почини обходом через review_labels.list_pages "
        "(fail loud на неожиданной форме ответа) или, если инстанс уже "
        "известен и чинится отдельно, назови причину в "
        "ALLOWLIST_KNOWN_INSTANCES этого файла."
    )


def test_review_labels_list_pages_itself_does_not_carry_the_pattern():
    """Мутация: верни в review_labels.list_pages старый узор
    (`if not isinstance(chunk, list) or not chunk: break`) — этот тест
    покраснеет первым, до общего сканирования выше (узкая, прицельная
    проверка канонической реализации самого класса)."""
    path = REPO_ROOT / "scripts" / "lib" / "review_labels.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and _matched_varname(node.test) is not None:
            assert not _stops_the_loop(node.body), (
                f"list_pages снова несёт снятый узор на строке {node.lineno}")


def test_allowlist_entries_still_carry_the_pattern_not_stale():
    """Обратная гвардия (симметрично allowlist-проверкам test_pagination_
    guard.py): запись allowlist, чей файл больше НЕ несёт узор (кто-то
    починил file_tasks.py, но забыл снять запись), — сама по себе не ошибка
    функционально, но означает забытую запись, которая скрыла бы РЕГРЕСС в
    другом месте того же файла впредь. Явная проверка честности реестра, не
    для основного сканирования выше."""
    stale = []
    for rel in ALLOWLIST_KNOWN_INSTANCES:
        path = REPO_ROOT / rel
        if not path.exists():
            stale.append(f"{rel} (файл не существует)")
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found = any(
            isinstance(node, ast.If) and _matched_varname(node.test) is not None
            and _stops_the_loop(node.body)
            for node in ast.walk(tree)
        )
        if not found:
            stale.append(f"{rel} (узор больше не найден — почини allowlist)")
    assert stale == [], f"Устаревшие записи ALLOWLIST_KNOWN_INSTANCES: {stale}"
