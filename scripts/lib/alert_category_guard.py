#!/usr/bin/env python3
"""Ни один сигнал владельцу не уходит без категории (#1461).

Класс одной фразой: **точка отправки, забывшая категорию, возвращает ровно то
состояние, ради ухода из которого категория и заводилась** — всё одним
потоком, решение владельца вперемешку с «деплой откатился».

Обязательный аргумент категории ловит это в рантайме — но только если строка
исполнилась. Сигналы уходят из редких веток (пульс не тикает, предохранитель
сработал, воркер провалился), и красный прогон случился бы у владельца в
чате, а не в CI. Поэтому проверка статическая, по исходнику — у ВСЕХ ТРЁХ
отправителей (находка ai-ревью этого PR: первая редакция покрывала один из
трёх, и сам PR прошёл свой гейт с пятью некатегоризованными вызовами):

* Python — `send_telegram(..., category=...)`, разбор AST;
* bash — `telegram_report <категория> <текст>` в `scripts/**/*.sh`,
  структурно: первый аргумент — слово из реестра, не переменная и не текст;
* TypeScript — `#telegramApi("sendMessage", { text: decorateAlert(...) })`
  в `cf-worker/src/**/*.ts`, структурно: тело sendMessage обязано нести
  `decorateAlert` с литералом из реестра.

Честная граница TS: `answerCallbackQuery` (тост-ответ на нажатие кнопки самим
владельцем) и `editMessageText` (правка УЖЕ отправленного и уже
категоризованного сообщения решения) нового сигнала в поток не создают и
гвардией не покрываются. Новый `sendMessage` мимо `decorateAlert` — красится.

Разбор Python — AST, не регулярка. Урок оплачен в этой же сессии (#1438):
текстовый поиск `send_telegram(` не видит многострочный вызов, где `category=`
стоит ниже по строкам, и красит зелёным ровно то, что обязан ловить. AST видит
вызов целиком независимо от переносов. Для bash и TS носитель тот же
(структурный тест по исходнику, в духе #308/#309), форма своя — см. докстринги
`_bash_calls_without_category` и `_ts_sends_without_category` ниже.

Газ (AGENTS.md, «тормоз без газа»): нарушение снимается добавлением
категории из реестра `scripts/lib/alert_category.py` — сообщение называет
файл, строку и причину. Реестра исключений здесь НЕТ намеренно: «отправить
без категории» не имеет законного случая, в отличие от шагов workflow, где
долг измерен и назван (api_quota_gate_guard).

Запуск: python scripts/lib/alert_category_guard.py
Тесты:  python -m pytest scripts/lib/test_alert_category_guard.py -q
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import ast
import re
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCANNED_DIRS = ("scripts",)
TS_DIR = "cf-worker/src"
SENDER_NAME = "send_telegram"
BASH_SENDER_NAME = "telegram_report"
TS_SENDER_NAME = "#telegramApi"
TS_NEW_MESSAGE_METHODS = ("sendMessage",)
CATEGORY_KEYWORD = "category"

_REGISTRY_MODULE = "alert_category"

_registry_cache = None


def _registry():
    """Настоящий модуль реестра, один раз на прогон. Гвардия читает
    alert_category.py, а не свою копию списка: новая категория правит одно
    место и не требует правки гвардии."""
    global _registry_cache
    if _registry_cache is None:
        spec = importlib.util.spec_from_file_location(
            _REGISTRY_MODULE, Path(__file__).resolve().parent / "alert_category.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _registry_cache = module
    return _registry_cache


def _category_value_problem(node: ast.AST) -> str | None:
    """Почему значение `category=` не годится, или None — годится.

    Проверять НАЛИЧИЕ аргумента мало, и это выяснилось на живой ошибке автора
    в этом же PR: я написал `category=pulse_guard.alert_category.OWNER_DECISION`
    в модуле, куда имя `pulse_guard` не импортировано. Аргумент был на месте,
    гвардия молчала — упало в рантайме NameError'ом на тестах. Тот же класс,
    что «имя есть, проводки нет», за который репозиторий платил не раз.

    Годятся ровно две формы:
      * `alert_category.<ИМЯ>` — константа реестра, ИМЯ сверяется с ним же:
        `alert_category.PIEPLEINE` проходит проверку «ссылается на реестр»,
        красит CI зелёным и падает AttributeError'ом в рантайме редкой ветки
        (находка ai-ревью этого PR), поэтому обязательна и сверка атрибута;
      * строковый литерал, который реестр знает.
    Всё остальное (переменная, вызов, f-строка) статически не разрешимо —
    честнее назвать это нарушением, чем пропустить молча."""
    if isinstance(node, ast.Attribute):
        base = node.value
        if isinstance(base, ast.Name) and base.id == _REGISTRY_MODULE:
            registry = _registry()
            value = getattr(registry, node.attr, None)
            if isinstance(value, str) and value in registry.CATEGORIES:
                return None
            known = ", ".join(_constant_names(registry))
            return (f"{_REGISTRY_MODULE}.{node.attr} не объявлена в "
                    f"{_REGISTRY_MODULE}.CATEGORIES — опечатка в имени константы "
                    f"прошла бы CI и упала в рантайме редкой ветки. Известны: {known}")
        return (f"значение `category=` ссылается не на реестр {_REGISTRY_MODULE} напрямую — "
                f"статически не проверить, что имя вообще разрешится (живой случай: "
                f"`pulse_guard.alert_category.X` в модуле без импорта pulse_guard)")
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        if node.value in _registry().CATEGORIES:
            return None
        return f"категория {node.value!r} не объявлена в {_REGISTRY_MODULE}.CATEGORIES"
    return ("значение `category=` не разрешается статически — ожидается "
            f"{_REGISTRY_MODULE}.<КОНСТАНТА> или строка из реестра")


def _constant_names(registry) -> list[str]:
    """Имена констант реестра (OWNER_DECISION…), не значения — подсказка в
    сообщении нарушения обязана быть копируемой дословно. Нестроковые
    атрибуты (сам CATEGORIES, CATEGORY_ORDER) в подсказку не попадают."""
    return sorted(
        name for name in dir(registry)
        if name.isupper() and isinstance(getattr(registry, name, None), str)
        and getattr(registry, name) in registry.CATEGORIES)


def _calls_without_category(source: str) -> list[tuple[int, str]]:
    """(строка, причина) для вызовов `send_telegram`, чья категория не годится.

    Определение самого отправителя (`def send_telegram`) вызовом не является и
    в список не попадает: иначе гвардия штрафовала бы файл, который эту
    возможность и предоставляет."""
    offenders: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name != SENDER_NAME:
            continue
        by_name = {kw.arg: kw.value for kw in node.keywords}
        # `**kwargs` даёт arg=None: считать такой вызов закрытым нельзя —
        # что там внутри, статически не видно, и это честнее назвать
        # нарушением, чем пропустить молча.
        if CATEGORY_KEYWORD not in by_name:
            offenders.append((node.lineno, f"нет `{CATEGORY_KEYWORD}=`"))
            continue
        problem = _category_value_problem(by_name[CATEGORY_KEYWORD])
        if problem:
            offenders.append((node.lineno, problem))
    return offenders


def check(repo_root: Path = REPO_ROOT) -> list[str]:
    problems: list[str] = []
    for directory in SCANNED_DIRS:
        for path in sorted((repo_root / directory).rglob("*.py")):
            if _is_test_path(path):
                continue  # тесты зовут отправитель со стендами, а не шлют владельцу
            try:
                source = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if SENDER_NAME not in source:
                continue
            for lineno, reason in _calls_without_category(source):
                rel = path.relative_to(repo_root)
                problems.append(
                    f"{rel}:{lineno}: {SENDER_NAME}(...) — {reason}. Сигнал уйдёт "
                    f"владельцу в общий поток или упадёт в рантайме. Категория берётся "
                    f"из scripts/lib/alert_category.py (#1461)")
    for directory in SCANNED_DIRS:
        for path in sorted((repo_root / directory).rglob("*.sh")):
            if _is_test_path(path):
                continue  # смок зовёт отправитель через заглушку, владельцу он не пишет
            try:
                source = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if BASH_SENDER_NAME not in source:
                continue
            for lineno, reason in _bash_calls_without_category(source):
                rel = path.relative_to(repo_root)
                problems.append(
                    f"{rel}:{lineno}: {BASH_SENDER_NAME}(...) — {reason}. Категория — "
                    f"первым аргументом, словом из scripts/lib/alert_category.py (#1461)")
    for path in sorted((repo_root / TS_DIR).rglob("*.ts")):
        if path.name.endswith((".spec.ts", ".test.ts")) or _is_test_path(path):
            continue  # спеки строят стенды и ловят моки, владельцу ничего не уходят
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if TS_SENDER_NAME not in source:
            continue
        for lineno, reason in _ts_sends_without_category(source):
            rel = path.relative_to(repo_root)
            problems.append(
                f"{rel}:{lineno}: {TS_SENDER_NAME}(\"sendMessage\", …) — {reason}. "
                f"Текст сообщения обязан идти через decorateAlert с категорией "
                f"из реестра (cf-worker/src/config.ts, #1461)")
    return problems


def _is_test_path(path: Path) -> bool:
    """Тестовый путь: каталог `test` где-то выше или имя на `test_`. Тесты
    зовут отправителя со стендами/заглушками — требовать категорию там значит
    штрафовать файл, который владельцу ничего не отправляет."""
    return "test" in path.parts[:-1] or path.name.startswith("test_")


# ── bash: первый аргумент telegram_report — слово из реестра ────────────────────

_BASH_CALL_RE = re.compile(
    r"\b" + BASH_SENDER_NAME + r"\b(?!\s*\()"   # определение `telegram_report() {` — не вызов
    r"\s+([^\s|;&)`\"']*)")                      # первый аргумент до разделителя или кавычки


def _bash_calls_without_category(source: str) -> list[tuple[int, str]]:
    r"""(строка, причина) для вызовов telegram_report без категории первым
    аргументом. bash-вызов переносится `\` — гвардия подклеивает строки,
    иначе перенос спрятал бы нарушение ровно так, как AST-разбор его ловит на
    Python. Комментарии и СТРОКИ срезаются с учётом кавычек: сами тексты
    сигналов несут `#` («задача #123»), а слово «telegram_report» звучит и в
    warning'ах самого отправителя — матчить его внутри кавычек значило бы
    красить живой код. Вызов при этом всегда стоит вне кавычек."""
    offenders: list[tuple[int, str]] = []
    for lineno, logical in _bash_logical_lines(source):
        stripped = _strip_bash_comment(logical)
        for match in _BASH_CALL_RE.finditer(_mask_quoted(stripped)):
            token = match.group(1)
            if token in _registry().CATEGORIES:
                continue
            if token:
                reason = (f"категория {token!r} не объявлена в реестре — "
                          f"alert_prefix откажет ДО сетевого вызова")
            else:
                reason = ("первый аргумент не разрешается статически — ожидается "
                          "слово из реестра (decision/breakage/pipeline/infra)")
            offenders.append((lineno, reason))
    return offenders


def _mask_quoted(line: str) -> str:
    r"""Строка с содержимым кавычек, заменённым на пробел. Экранированный
    символ внутри двойных кавычек кавычку не закрывает (`\"`). Слово,
    совпавшее внутри строки-аргумента, — не вызов, и matcher по маске его
    не видит."""
    buf: list[str] = []
    quote = None
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == "\\" and quote == '"':
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            buf.append(" ")
        else:
            buf.append(ch)
        i += 1
    return "".join(buf)


def _bash_logical_lines(source: str) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    buf = ""
    start = 0
    for lineno, raw in enumerate(source.splitlines(), start=1):
        if buf:
            buf += raw
        else:
            buf = raw
            start = lineno
        if buf.rstrip().endswith("\\"):
            buf = buf.rstrip()[:-1] + " "
            continue
        lines.append((start, buf))
        buf = ""
    if buf:
        lines.append((start, buf))
    return lines


def _strip_bash_comment(line: str) -> str:
    r"""Хвост `# …` срезан; `#` внутри кавычек («задача #123») — не комментарий."""
    quote = None
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == "\\" and quote == '"':
                i += 2  # экранированный символ кавычку не закрывает
                continue
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i]
        i += 1
    return line


# ── TypeScript: sendMessage без decorateAlert ───────────────────────────────────

_TS_TEXT_KEY_RE = re.compile(r"\btext:\s*")
_TS_DECORATE_LITERAL_RE = re.compile(r"decorateAlert\(\s*[\"']([a-z_]+)[\"']")
_TS_DECORATE_NAME_RE = re.compile(r"decorateAlert\b")


def _ts_sends_without_category(source: str) -> list[tuple[int, str]]:
    """(строка, причина) для sendMessage без категории. Методы-НЕ-sendMessage
    (answerCallbackQuery/editMessageText) — не новые сигналы в поток, см.
    докстринг модуля; их здесь нет по построению."""
    offenders: list[tuple[int, str]] = []
    for lineno, body in _ts_call_bodies(source):
        if not re.match(r"\s*[\"'](" + "|".join(TS_NEW_MESSAGE_METHODS) + r")[\"']\s*,", body):
            continue
        key = _TS_TEXT_KEY_RE.search(body)
        if not key:
            offenders.append((lineno, "не видно text= — статически не проверить, что "
                                      "сообщение несёт категорию"))
            continue
        rest = body[key.end():]
        literal = _TS_DECORATE_LITERAL_RE.match(rest)
        if literal:
            if literal.group(1) in _registry().CATEGORIES:
                continue
            offenders.append((lineno, f"decorateAlert с категорией {literal.group(1)!r}, "
                                      f"которой нет в реестре"))
        elif _TS_DECORATE_NAME_RE.match(rest):
            offenders.append((lineno, "категория в decorateAlert не строковый литерал — "
                                      "статически не разрешается"))
        else:
            offenders.append((lineno, "text собирается без decorateAlert — сигнал уйдёт "
                                      "владельцу в общий поток"))
    return offenders


def _ts_call_bodies(source: str):
    """(строка, тело вызова) для каждого `#telegramApi( … )` — тело вырезано по
    балансу скобок; строковые литералы пропускаются целиком, чтобы скобка в
    тексте сообщения не обрывала вырезание на середине payload'а."""
    for match in re.finditer(re.escape(TS_SENDER_NAME) + r"\(", source):
        depth = 1
        i = match.end()
        quote = None
        while i < len(source) and depth:
            ch = source[i]
            if quote:
                if ch == "\\":
                    i += 2
                    continue
                if ch == quote:
                    quote = None
            elif ch in "\"'`":
                quote = ch
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            i += 1
        yield source[:match.start()].count("\n") + 1, source[match.end():i - 1]


def main() -> int:
    problems = check()
    if problems:
        for problem in problems:
            print(f"::error::alert-category: {problem}")
        return 1
    print("alert-category: каждый вызов отправителя несёт категорию")
    return 0


if __name__ == "__main__":
    sys.exit(main())
