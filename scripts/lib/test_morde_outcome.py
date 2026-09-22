#!/usr/bin/env python3
"""Гвардия единого места правды по исходу вызова морды (#1433).

Класс, оплаченный тремя авариями за одни сутки 2026-09-21/22: каждый вызывающий
классифицировал отказ своим рукописным правилом и не знал о чужих. Архив знал
`session-not-found`, заметки знали `HTTP 404`, и обе ждали повтора там, где
морда отказывается мигрировать формат сессии — повтора, которого не будет
никогда (#1430). Каждая авария учила ОДНО место; остальные оставались слепыми.

Здесь две половины:

* поведение классификатора на ПРОД-ФОРМАХ отказов, скопированных с живых
  прогонов (не пересказанных);
* гвардия по исходнику `scheduler.py`: новой рукописной копии правила там не
  появляется. Это структурная проверка сознательно — утверждение «во всём
  файле нет второй копии» про ОТСУТСТВИЕ формы, а не про поведение одного
  вызова; прецедент той же формы — `scripts/lib/test_pagination_guard.py`.

Запуск: python -m pytest scripts/lib/test_morde_outcome.py -q
"""

import ast
import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEDULER = REPO_ROOT / "scripts" / "orchestra" / "scheduler.py"

_spec = importlib.util.spec_from_file_location(
    "morde_outcome", Path(__file__).with_name("morde_outcome.py"))
mo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mo)


# ── Прод-формы отказов: скопированы с живых прогонов ────────────────────────

# Прогон 35647962282, 2026-09-22T00:25Z — заметки-итоги в сессию harness-1251.
UNMIGRATABLE = (
    'HTTP 500: {"ok":false,"error":"Internal runtime error.","code":"internal",'
    '"detail":"SessionFormatUnsupportedMigrationError: '
    '@deepseek-ai/dsh-session-format-v0-to-v1 refuses this format v0 Session: '
    'tool/result 67 data has unexpected member \\"truncated\\""}'
)

# Прогон 35627959443, 2026-09-21T16:48Z — исчерпанная суточная квота DO.
QUOTA_EXHAUSTED = (
    'HTTP 500: {"ok":false,"error":"Internal runtime error.","code":"internal",'
    '"detail":"Error: Exceeded allowed rows read in Durable Objects free tier."}'
)

# Прогон 35671863172, 2026-09-22 — архив сессии, которой в морде нет.
SESSION_MISSING_RPC = "RPC workspace.archiveSession отклонён: session-not-found"
SESSION_MISSING_HTTP = "HTTP 404: {\"ok\":false,\"error\":\"not found\"}"


def test_unmigratable_format_is_terminal():
    """Формат в хранилище морды сам не изменится — повтор не поможет никогда."""
    assert mo.is_terminal(UNMIGRATABLE) is True
    assert mo.terminal_reason(UNMIGRATABLE) == (
        "морда отказывается мигрировать формат сессии — формат в хранилище сам не изменится")


def test_quota_exhaustion_is_broken_not_terminal():
    """Важнее первого теста: тот же HTTP 500, но повтор ПОМОЖЕТ — суточная
    квота DO сбрасывается в 00:00 UTC. Замолчать его значило бы потерять
    сигнал о реально сломанной морде, «починив» соседний случай глушилкой по
    коду ответа."""
    assert mo.classify(QUOTA_EXHAUSTED) == mo.BROKEN
    assert mo.terminal_reason(QUOTA_EXHAUSTED) is None


def test_missing_session_is_terminal_on_both_transports():
    """Один смысл, два транспорта: RPC отвечает текстом, ingest — кодом HTTP.
    До #1433 про каждый знал ровно один вызывающий."""
    assert mo.is_terminal(SESSION_MISSING_RPC) is True
    assert mo.is_terminal(SESSION_MISSING_HTTP) is True


def test_unknown_failure_defaults_to_broken():
    """Умолчание — громкое. Незнакомый отказ громче, чем надо, стоит лишнего
    сигнала; тише, чем надо — молчаливого накопления. Тот же fail-safe, что у
    active_run_kind в wake_orchestra.sh (#1408)."""
    assert mo.classify("HTTP 502: bad gateway") == mo.BROKEN
    assert mo.classify("timed out after 30s") == mo.BROKEN
    assert mo.classify("") == mo.BROKEN


def test_adding_a_marker_reaches_every_caller_at_once(monkeypatch):
    """Смысл всей задачи: новый терминальный отказ дописывается ОДНОЙ строкой,
    и его начинают одинаково понимать все потребители — без правки их кода.

    До #1433 добавить отказ значило найти все рукописные копии и не забыть ни
    одной; забытая копия и была механизмом каждой из трёх аварий."""
    novel = "SomeBrandNewUpstreamRefusal"
    assert mo.classify(novel) == mo.BROKEN, "до добавления — обычная поломка"

    monkeypatch.setitem(mo.TERMINAL_FAILURES, novel, ("новый апстримный отказ", mo.LOSS))

    assert mo.is_terminal(f"HTTP 500: {novel}: подробности") is True
    assert mo.terminal_reason(f"HTTP 500: {novel}: подробности") == "новый апстримный отказ"
    assert mo.is_loss(f"HTTP 500: {novel}: подробности") is True


def test_terminal_outcomes_split_into_normal_and_loss():
    """Оба «повтор не поможет», но отчёт у них разный, и это различение
    поймал живой тест, а не рассуждение: `test_append_session_notes_session_
    not_found_is_not_hard_failure` требует, чтобы отсутствующая сессия НЕ
    давала строки. Задача без прогона воркера (ручной PR, работа агента в
    чате) сессии в морде не имеет вовсе — терять нечего. Немигрируемый формат
    — потеря: заметка не доедет никогда, и молчать о ней нельзя."""
    assert mo.is_loss(SESSION_MISSING_HTTP) is False
    assert mo.is_loss(SESSION_MISSING_RPC) is False
    assert mo.is_loss(UNMIGRATABLE) is True
    # Нетерминальный отказ потерей не считается: там повтор ещё поможет.
    assert mo.is_loss(QUOTA_EXHAUSTED) is False


# ── Гвардия: второй копии правила в scheduler.py не появляется ──────────────
#
# Прежняя редакция зашивала альтернацию из ЧЕТЫРЁХ известных маркеров
# (`HTTP 404`, `session-not-found`, …) — и была слепа ровно на тот сценарий,
# класс которого закрывает (находка ai-review PR #1434). Все три исторические
# копии родились одинаково: каждая писала СВОЙ свежий маркер, которого до неё
# в списке не было. Гвардия по списку известных их бы не заметила ни одну.
#
# Поэтому признак теперь бесмаркерный, а область — узкая: ЛЮБОЕ сопоставление
# текста ошибки со строковым литералом (`in`/`not in`, `==`/`!=`,
# `.startswith`-семейство, `re.search/match/…` со строковым паттерном) внутри
# тел функций, которые ходят в морду. Формы `==` и `re.search` добавлены тем
# же ревью вторым заходом: прежний признак разбирал только `in` и
# `startswith`, а текст отказа называл правило обобщённо — честный
# разработчик уходил ровно в непокрытые формы и молча возвращал класс.
# Узость обязательна: `in str(` встречается в scheduler.py и по
# другим поводам (например проверка содержимого ответа gh), и бесмаркерный
# признак по всему файлу давал бы ложные срабатывания — то есть гвардию,
# которую начнут глушить.


# Двери морды в scheduler.py — единственные обёртки, через которые файл
# ходит в dsh-edge. Область гвардии выводится из ИСХОДНИКА (функции с
# вызовом двери или потреблением `morde_outcome`), а не хардкодится списком
# имён: хардкод двух имён покрывал два из семи ходоков, и четвёртая копия
# правила в любом из остальных пяти мест прошла бы молча (находка ai-review
# PR #1434). Новый вызывающий морды попадает в область сам, без правки
# гвардии.
MORDE_DOORS = frozenset({"_morde_opener", "_morde_login", "_morde_rpc", "_morde_ingest"})


def _morde_going_function_names(source: str) -> list[str]:
    """Имена всех функций scheduler.py, которые ходят в морду: вызывают
    дверь `_morde_*`, потребляют общий классификатор `morde_outcome` или
    упоминают двери в своём тексте (упоминание — тоже маркер «функция живёт
    на маршруте морды», как `post_entity_snapshot`, гейтящий сырые записи
    той же `_guard_raw_subprocess_write`, что `_morde_rpc`)."""
    tree = ast.parse(source)
    names: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        called = {
            n.func.id
            for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        touched = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
        segment = ast.get_source_segment(source, node) or ""
        if (called & MORDE_DOORS or "morde_outcome" in touched
                or "_morde_" in segment):
            names.append(node.name)
    return names


def _string_comparisons_against_error(source: str, function_name: str) -> list[str]:
    """Строковые литералы, с которыми тело `function_name` классифицирует текст
    ошибки. Любой такой литерал — рукописная классификация отказа, то есть
    вторая копия правила, где бы она ни появилась и как бы ни назывался её
    маркер.

    Разбор по AST, а не регуляркой: форма записи (`in str(error)`,
    `in f"{error}"`, `in message`, перенос на две строки) меняется свободно,
    а смысл — нет. Регулярка ловила бы форму и промахивалась бы по смыслу,
    что с прежней редакцией и случилось.

    Покрытые формы (и ровно они названы в тексте отказа гвардии — обещание
    не шире механизма, находка ai-review PR #1434: гвардия молча зеленела на
    `==` и `re.search`, при том что её собственное сообщение называло правило
    обобщённо и честный разработчик уходил ровно в эти формы):

    * `"литерал" in/not in <текст ошибки>`;
    * `<текст ошибки> ==/!= "литерал"` — литерал с ЛЮБОЙ стороны;
    * `<текст ошибки>.startswith/.endswith/.find/.index/.count("литерал")`;
    * `re.search/match/fullmatch/findall/finditer("паттерн", <текст
      ошибки>)` и `паттерн_объект.search(…)` — строковый литерал первым
      аргументом.

    Честный потолок: паттерн, собранный заранее (`re.compile(r"…")`) и
    вызванный одним аргументом (`compiled(str(error))`), признак не видит —
    текст отказа называет покрытые формы, а не «любые»."""
    tree = ast.parse(source)
    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            target = node
            break
    assert target is not None, f"функция {function_name} исчезла из scheduler.py"

    found: list[str] = []
    for node in ast.walk(target):
        if isinstance(node, ast.Compare):
            for op, comparator in zip(node.ops, node.comparators):
                left = node.left
                # `"литерал" in <что-то>` и `"литерал" not in <что-то>`
                if isinstance(op, (ast.In, ast.NotIn)):
                    if isinstance(left, ast.Constant) and isinstance(left.value, str):
                        if _mentions_error(comparator):
                            found.append(left.value)
                # `<текст ошибки> == "литерал"` — и литерал с любой из двух
                # сторон: `if "session-not-found" == str(error):` — та же
                # классификация, читается она так же.
                if isinstance(op, (ast.Eq, ast.NotEq)):
                    if (isinstance(left, ast.Constant) and isinstance(left.value, str)
                            and _mentions_error(comparator)):
                        found.append(left.value)
                    elif (isinstance(comparator, ast.Constant)
                          and isinstance(comparator.value, str)
                          and _mentions_error(left)):
                        found.append(comparator.value)
        # `str(error).startswith("литерал")` / `.endswith(...)` — та же классификация
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in ("startswith", "endswith", "find", "index", "count"):
                if _mentions_error(node.func.value):
                    for arg in node.args:
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                            found.append(arg.value)
            # `re.search(r"паттерн", str(error))` — регэксп повторяет ту же
            # подстрочную классификацию, только в другой форме записи; объект
            # не обязан быть именно модулем `re` — собранный заранее паттерн
            # (`compiled.search("литерал", текст)`) ловится тем же признаком.
            if node.func.attr in ("search", "match", "fullmatch", "findall", "finditer"):
                if (len(node.args) >= 2
                        and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, str)
                        and _mentions_error(node.args[1])):
                    found.append(node.args[0].value)
    return found


def _mentions_error(node: ast.expr) -> bool:
    """Упоминает ли выражение переменную ошибки — под любым из принятых в
    файле имён. Имя проверяется по вхождению, а не по точному совпадению:
    `str(error)`, `f"{error}"`, `message`, `detail` — всё это текст отказа."""
    names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
    return bool(names & {"error", "err", "exc", "message", "detail", "text"})


def test_scheduler_has_no_handwritten_outcome_classification():
    """Место правды одно. Новая копия правила — отложенный рецидив: она будет
    знать про свой отказ и не знать про остальные, ровно как три прежние.

    Мутация, доказывающая бесмаркерность: вписать в `_archive_with_opener`
    условие с ЛЮБЫМ новым, здесь не перечисленным маркером — тест краснеет
    (прежняя редакция такую копию пропускала молча)."""
    source = SCHEDULER.read_text(encoding="utf-8")
    offenders = {}
    for name in _morde_going_function_names(source):
        literals = _string_comparisons_against_error(source, name)
        if literals:
            offenders[name] = literals
    assert offenders == {}, (
        "в scheduler.py снова рукописная классификация отказа морды — текст "
        "ошибки сопоставляется со строковым литералом (in/not in, ==/!=, "
        "startswith/…, re.search/match/…) прямо в теле функции, ходящей в "
        f"морду (место правды — scripts/lib/morde_outcome.py, #1433): {offenders}"
    )


def test_the_guard_sees_a_marker_it_has_never_heard_of():
    """Главный тест этой пары, и он про саму гвардию, а не про scheduler.py.

    Прежняя редакция зашивала список известных маркеров — и пропускала
    ЧЕТВЁРТУЮ копию с новым маркером, то есть ровно тот способ, которым
    родились все три исторические (находка ai-review PR #1434). Здесь
    проверяется, что признак бесмаркерный ВО ВСЕХ заявленных формах записи:
    литералы придуманы только что и ни в одном списке репозитория не
    значатся. Второй заход того же ревью: формы `==` и `re.search` гвардия
    пропускала молча, хотя её собственное сообщение называло правило
    обобщённо, — теперь каждая форма ловится."""
    source = (
        "def _archive_with_opener(repo, opener, task_numbers):\n"
        "    try:\n"
        "        pass\n"
        "    except RuntimeError as error:\n"
        '        if "СовершенноНовыйОтказКоторогоНиктоНеВидел" in str(error):\n'
        "            return []\n"
        '        if str(error) == "РавенствоНовойФормыТожеНигдеНеВиденное":\n'
        "            return []\n"
        '        if re.search(r"РегэксповаяФормаИНоваяКоторуюНеВидали", str(error)):\n'
        "            return []\n"
    )
    assert _string_comparisons_against_error(source, "_archive_with_opener") == [
        "СовершенноНовыйОтказКоторогоНиктоНеВидел",
        "РавенствоНовойФормыТожеНигдеНеВиденное",
        "РегэксповаяФормаИНоваяКоторуюНеВидали",
    ]


def test_the_guard_sees_equality_comparison():
    """Форма `==`. Её нашло ai-ревью PR #1434 ИСПОЛНЕНИЕМ: признак разбирал
    только `in`/`startswith`, и копия проходила молча.

    Тест здесь не дублирует мутацию из шапки гвардии, а страхует её: шапка —
    текст, и в этом файле она уже дважды расходилась с деревом. Сузь кто-нибудь
    `_string_comparisons_against_error` обратно — покраснеет этот тест, а не
    только ручной перегон."""
    source = (
        "def _archive_with_opener(repo, opener, task_numbers):\n"
        "    try:\n"
        "        pass\n"
        "    except RuntimeError as error:\n"
        '        if str(error) == "session-not-found":\n'
        "            return []\n"
    )
    assert _string_comparisons_against_error(source, "_archive_with_opener") == [
        "session-not-found"
    ]


def test_the_guard_sees_equality_with_the_literal_on_the_left():
    """Порядок операндов в Python свободен: полагаться на привычку автора
    значило бы оставить вторую дверь вплотную к первой."""
    source = (
        "def _archive_with_opener(repo, opener, task_numbers):\n"
        "    try:\n"
        "        pass\n"
        "    except RuntimeError as error:\n"
        '        if "session-not-found" != str(error):\n'
        "            return []\n"
    )
    assert _string_comparisons_against_error(source, "_archive_with_opener") == [
        "session-not-found"
    ]


def test_the_guard_sees_a_regular_expression_over_the_error_text():
    """Вторая форма того же круга ревью: регулярка по тексту отказа — та же
    классификация, только записанная дороже."""
    source = (
        "def _archive_with_opener(repo, opener, task_numbers):\n"
        "    try:\n"
        "        pass\n"
        "    except RuntimeError as error:\n"
        '        if re.search(r"session-not-found", str(error)):\n'
        "            return []\n"
    )
    assert _string_comparisons_against_error(source, "_archive_with_opener") == [
        "session-not-found"
    ]


def test_precompiled_pattern_is_the_declared_blind_spot_not_an_accident():
    """Честный потолок, закреплённый машиной, а не только прозой.

    Докстринг признака называет непокрытую форму прямо: паттерн, собранный
    заранее (`re.compile`) и вызванный одним аргументом. Пока потолок только
    в тексте, он живёт до первой правки; этот тест делает его наблюдаемым
    состоянием. Покраснеет он в двух случаях, и оба — работа, а не поломка:
    форму покрыли (тогда потолок из докстринга надо убрать) или признак
    сломали так, что он стал ловить лишнее."""
    source = (
        "def _archive_with_opener(repo, opener, task_numbers):\n"
        "    try:\n"
        "        pass\n"
        "    except RuntimeError as error:\n"
        "        if _NOT_FOUND_RE.search(str(error)):\n"
        "            return []\n"
    )
    assert _string_comparisons_against_error(source, "_archive_with_opener") == []


def test_the_guard_does_not_fire_on_calls_that_are_not_about_the_error():
    """Обратная сторона: узость области — не формальность. Сравнение строки
    с чем-то, что не является текстом отказа, копией правила не является, и
    ложное срабатывание здесь стоило бы дороже пропуска: гвардию, которая
    кричит не по делу, начинают глушить. Формы `==` и `re.search` проверены
    на обеих сторонах — живой `kind == …` из post_entity_snapshot и поиск по
    не-ошибочной строке молчание гвардии не ломают."""
    source = (
        "def _archive_with_opener(repo, opener, task_numbers):\n"
        '    if "harness-" in session_id:\n'
        "        return []\n"
        '    if kind == "model":\n'
        "        return []\n"
        '    return re.search("harness-", repo)\n'
    )
    assert _string_comparisons_against_error(source, "_archive_with_opener") == []


def test_scheduler_actually_uses_the_shared_classifier():
    """Обратная сторона первой гвардии: она зеленеет и тогда, когда
    классификации не осталось ВООБЩЕ — например если вызывающий просто
    перестал различать исходы. Значит нужна и проверка, что общий
    классификатор реально зовут."""
    text = SCHEDULER.read_text(encoding="utf-8")
    assert "morde_outcome.is_terminal(" in text, (
        "scheduler.py не зовёт общий классификатор — различение исходов пропало"
    )
    assert text.count("morde_outcome.is_terminal(") >= 2, (
        "общий классификатор зовут меньше чем из двух мест: архив и заметки — "
        "оба обязаны идти через него"
    )
