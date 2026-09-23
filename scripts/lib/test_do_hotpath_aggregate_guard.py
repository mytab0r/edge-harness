#!/usr/bin/env python3
"""Гвардия класса «неограниченный агрегат на горячем пути DO» (#320/#321/#575).

Класс: `cf-worker/src/harness.ts#status()` вызывается на КАЖДЫЙ heartbeat,
батч событий, приём сообщения инбокса, callback-кнопку — горячий путь по
определению (спека `do-rows-read-quota` 14.5 — распространена этим же PR на
`messages`, не 14.4: тот пункт про индекс watchdog-запроса, другое
требование). Полный `GROUP BY status` по таблице без фильтра
читает ряды пропорционально её размеру: #320 подпалил квоту rows_read на
`tasks` (закрыто кэшем `#taskCounts`, PR #321), #575 — та же болезнь на
`messages` (`messages` вдобавок не имела ретеншена вовсе, растёт вечно, см.
спека `do-sqlite-retention` 14.10/14.11).

Правило (общее для обеих таблиц, не дублируется на третью пофакту): любой
`GROUP BY` в этом файле обязан жить ВНУТРИ одного из кэш-геттеров
(`#taskCounts`/`#msgCounts`), не быть вызванным инлайн в `#status()` или
где-либо ещё. Если оба геттера потеряют актуальность — список ALLOWED_GETTERS
ниже правится сознательно (как EXPECTED_WORKFLOWS у test_dispatch_token_usage.py),
не молча.

Докажи мутацией: подставь код `#msgCounts()` инлайном в тело `#status()` (то
есть верни #575 к состоянию до фикса) — `test_status_body_has_no_inline_group_by`
покраснеет, потому что `GROUP BY` появится в теле #status().

Запуск: python -m pytest scripts/lib/test_do_hotpath_aggregate_guard.py -q
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS_TS = REPO_ROOT / "cf-worker" / "src" / "harness.ts"

# Единственные санкционированные владельцы прямого GROUP BY. Значение — ключ
# ВИДА агрегата в таблице `counts_cache`, которым геттер обязан и читать, и
# писать (см. test_cached_getters_are_actually_gated).
#
# До #1487 здесь стояло имя ПОЛЯ ОБЪЕКТА (`#taskCountsCache`), и это оказалось
# проверкой не того: поле живёт в памяти, а DO выгружается через ~10 с простоя,
# поэтому кэш был холодным почти на каждом запросе — гвардия была зелёной, пока
# счётчики пересчитывались полным GROUP BY по 6 900 строк на вызов. Хранилище
# переехало в SQL (`counts_cache`, рядом с `pulse`/`retention_state`/
# `storage_probe`, которые живут там по этой же причине), и проверяется теперь
# именно оно.
ALLOWED_GETTERS = {
    "#taskCounts": "tasks",
    "#msgCounts": "messages",
}


def harness_text() -> str:
    return HARNESS_TS.read_text(encoding="utf-8")


def extract_method_body(source: str, signature_re: str) -> str:
    """Тело метода от открывающей `{` сигнатуры до парной закрывающей —
    подсчётом скобок, не жадным regex (тело метода само содержит `{`/`}`)."""
    match = re.search(signature_re, source)
    assert match, f"метод не найден по сигнатуре: {signature_re!r} — harness.ts переименован/реструктурирован?"
    start = source.index("{", match.end() - 1)
    depth = 0
    i = start
    while i < len(source):
        ch = source[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start:i + 1]
        i += 1
    raise AssertionError(f"не нашёл закрывающую скобку метода {signature_re!r}")


# Строковый литерал SQL, не упоминание в прозе/комментарии/докстринге: `GROUP
# BY` обязан лежать МЕЖДУ парой одинаковых кавычек на одной строке — двойные,
# одинарные или бэктики (шаблонный литерал, как INSERT в retention_state в
# этом же файле). Колонка(и) после GROUP BY — любой текст без этой кавычки:
# один `\w+"` сразу после слова не покрывал `GROUP BY status, kind` и
# `` `...GROUP BY status`` `` (нашла ai-review, #575) — оба проходили гвардию
# молча.
SQL_GROUP_BY_RE = re.compile(r'"[^"]*GROUP BY[^"]*"|\'[^\']*GROUP BY[^\']*\'|`[^`]*GROUP BY[^`]*`')


def test_group_by_appears_only_in_allowed_getters():
    """Ровно столько GROUP BY-запросов в файле, сколько знает этот список
    (#320/#575) — считаются только строковые литералы SQL, не упоминания в
    комментариях/докстрингах (их в этом файле по конструкции много, включая
    сам этот).

    Новый GROUP BY-запрос где угодно ещё в harness.ts — сознательная правка
    ЭТОГО списка (после того, как её кэш-геттер доказан тестом ниже), не тихий
    обход."""
    text = harness_text()
    group_by_lines = [line.strip() for line in text.splitlines() if SQL_GROUP_BY_RE.search(line)]
    assert len(group_by_lines) == len(ALLOWED_GETTERS), (
        f"количество GROUP BY в harness.ts изменилось: {group_by_lines} — "
        f"ожидалось ровно {len(ALLOWED_GETTERS)} (по одному на {sorted(ALLOWED_GETTERS)}); "
        "новый полный агрегат обязан жить за собственным кэш-геттером (класс #320/#575), "
        "и только тогда список ALLOWED_GETTERS правится сознательно"
    )


# Агрегат по НЕиндексированной колонке — та же цена, что полный GROUP BY, но
# regex выше его не видит: `GROUP BY` в запросе нет (#1411, вторая дверь,
# найденная ai-review PR #1425). Живой случай: `#emitSystemEvent` брал
# следующий отрицательный seq через
# `SELECT COUNT(*) … FROM events WHERE task_id = ? AND source = 'system'` —
# `source` ни в одном индексе не ведёт, значит читались ВСЕ события задачи, и
# цена системного события росла вместе с длиной сессии. Замер на 100 000
# событий одной задачи: 14.25 мс → 0.003 мс после замены на
# `SELECT MIN(seq) … WHERE task_id = ?` (один seek по префиксу
# UNIQUE(task_id, seq), без нового индекса).
#
# Гвардия структурная сознательно, и это названо вслух: утверждение здесь —
# «во всём harness.ts нет ни одного агрегата по events, фильтрованного по
# source», то есть про ОТСУТСТВИЕ формы, а не про поведение одного вызова.
# Поведенческая половина живёт отдельно (cf-worker/test/journal-rows-read.spec.ts
# меряет rowsRead настоящего SqlStorage); прецедент той же формы —
# scripts/lib/test_pagination_guard.py.
AGGREGATE_BY_SOURCE_RE = re.compile(
    r'["\'`][^"\'`]*(?:COUNT|SUM|MIN|MAX)\s*\([^)]*\)[^"\'`]*FROM\s+events[^"\'`]*source\s*=',
)


def test_no_aggregate_over_events_filtered_by_source():
    """Агрегат по events с фильтром по `source` читает все события задачи —
    цена растёт вместе с сессией, как до #1411. Нужен счёт системных событий —
    бери его из того, что индекс уже упорядочил (`MIN(seq)` по префиксу
    task_id), а не пересчитывай таблицу."""
    offenders = AGGREGATE_BY_SOURCE_RE.findall(harness_text())
    assert offenders == [], (
        "агрегат по events, фильтрованный по неиндексированному source "
        f"(цена растёт с длиной сессии, #1411): {offenders}"
    )


def test_status_body_has_no_inline_group_by():
    """#status() — сам горячий путь — не имеет права звать GROUP BY напрямую.

    Это и есть регрессия #575: до фикса #status() делал
    `SELECT status, COUNT(*) ... FROM messages GROUP BY status` прямо в своём
    теле, минуя кэш (`#taskCounts()`/`#msgCounts()` — единственные легальные
    источники агрегата, см. тест ниже)."""
    status_body = extract_method_body(harness_text(), r"#status\(\):\s*Status\s*\{")
    assert "GROUP BY" not in status_body, (
        "#status() содержит GROUP BY инлайн — та же регрессия, что #320 (tasks) "
        "и #575 (messages): агрегат обязан идти через кэш-геттер (#taskCounts/#msgCounts), "
        "не пересчитываться на каждый вызов #status()"
    )


def test_cached_getters_are_actually_gated():
    """Каждый санкционированный геттер и правда кэширован, причём кэшем,
    ПЕРЕЖИВАЮЩИМ выгрузку DO из памяти: читает `#cachedCounts(<вид>)` и пишет
    `#storeCounts(<вид>, …)`, то есть строку в SQL, а не поле объекта.

    Обе половины обязательны. Только чтение без записи — кэш, который никогда
    не наполнится; только запись без чтения — GROUP BY на каждый вызов при
    исправно растущей таблице. Ровно это и был #1487: поле-кэш существовало и
    присваивалось, но между запросами объект успевал выгрузиться (~10 с
    простоя, #329), и защита не срабатывала НИ РАЗУ при зелёной гвардии.

    Гвардия структурная, и это названо вслух: она утверждает «в геттере есть
    обе операции», а не «кэш реально попадает». Поведенческая половина живёт
    в `cf-worker/test/harness.spec.ts` — там через `runInDurableObject` читается
    НАСТОЯЩАЯ строка `counts_cache` после запроса; «результат совпал» совпадает
    и на полностью холодном кэше, поэтому проверять надо факт в хранилище."""
    text = harness_text()
    for getter, kind in ALLOWED_GETTERS.items():
        body = extract_method_body(text, rf"{re.escape(getter)}\([^)]*\):[^{{]*\{{")
        assert "GROUP BY" in body, f"{getter}() больше не содержит GROUP BY — обнови ALLOWED_GETTERS"
        assert f'#cachedCounts("{kind}")' in body, (
            f'{getter}() не читает #cachedCounts("{kind}") — GROUP BY внутри него выполняется '
            "на каждый вызов, кэш существует только по имени (класс #320/#575/#1487)"
        )
        assert f'#storeCounts("{kind}"' in body, (
            f'{getter}() не пишет #storeCounts("{kind}", …) — посчитанный агрегат никуда не '
            "сохраняется, следующий вызов считает заново (класс #1487)"
        )


def test_status_calls_both_cached_getters():
    """#status() обязан брать счётчики ИМЕННО через кэш-геттеры, а не читать
    таблицы каким-то третьим путём в обход них."""
    status_body = extract_method_body(harness_text(), r"#status\(\):\s*Status\s*\{")
    for getter in ALLOWED_GETTERS:
        assert f"this.{getter}()" in status_body, (
            f"#status() не вызывает this.{getter}() — счётчики берутся откуда-то ещё "
            "(в обход кэша, класс #320/#575) или геттер переименован без обновления гвардии"
        )
