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

# Единственные санкционированные владельцы прямого GROUP BY — каждый обязан
# быть гейтед своим кэш-полем (см. test_cached_getters_are_actually_gated).
ALLOWED_GETTERS = {
    "#taskCounts": "#taskCountsCache",
    "#msgCounts": "#msgCountsCache",
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
    """Каждый санкционированный геттер и правда кэширован (`if (cache === null)`),
    не просто содержит GROUP BY без защиты — иначе он ничем не лучше инлайна."""
    text = harness_text()
    for getter, cache_field in ALLOWED_GETTERS.items():
        body = extract_method_body(text, rf"{re.escape(getter)}\([^)]*\):[^{{]*\{{")
        assert "GROUP BY" in body, f"{getter}() больше не содержит GROUP BY — обнови ALLOWED_GETTERS"
        assert f"{cache_field} === null" in body, (
            f"{getter}() потерял проверку `{cache_field} === null` — GROUP BY внутри него "
            "выполняется на каждый вызов, кэш существует только по имени (класс #320/#575)"
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
