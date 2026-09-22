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
# Поэтому признак теперь бесмаркерный, а область — узкая: ЛЮБОЕ сравнение
# текста ошибки со строковым литералом внутри тел функций, которые ходят в
# морду. Узость обязательна: `in str(` встречается в scheduler.py и по
# другим поводам (например проверка содержимого ответа gh), и бесмаркерный
# признак по всему файлу давал бы ложные срабатывания — то есть гвардию,
# которую начнут глушить.


FUNCTIONS_THAT_CALL_THE_MORDE = ("_archive_with_opener", "append_session_notes")


def _string_comparisons_against_error(source: str, function_name: str) -> list[str]:
    """Строковые литералы, с которыми тело `function_name` сравнивает текст
    ошибки. Любой такой литерал — рукописная классификация отказа, то есть
    вторая копия правила, где бы она ни появилась и как бы ни назывался её
    маркер.

    Разбор по AST, а не регуляркой: форма записи (`in str(error)`,
    `in f"{error}"`, `in message`, перенос на две строки) меняется свободно,
    а смысл — нет. Регулярка ловила бы форму и промахивалась бы по смыслу,
    что с прежней редакцией и случилось."""
    tree = ast.parse(source)
    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            target = node
            break
    assert target is not None, f"функция {function_name} исчезла из scheduler.py"

    found: list[str] = []
    for node in ast.walk(target):
        # `"литерал" in <что-то>` и `"литерал" not in <что-то>`
        if isinstance(node, ast.Compare):
            for op, comparator in zip(node.ops, node.comparators):
                if not isinstance(op, (ast.In, ast.NotIn)):
                    continue
                left = node.left
                if isinstance(left, ast.Constant) and isinstance(left.value, str):
                    if _mentions_error(comparator):
                        found.append(left.value)
        # `str(error).startswith("литерал")` / `.endswith(...)` — та же классификация
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in ("startswith", "endswith", "find", "index", "count"):
                if _mentions_error(node.func.value):
                    for arg in node.args:
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                            found.append(arg.value)
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
    for name in FUNCTIONS_THAT_CALL_THE_MORDE:
        literals = _string_comparisons_against_error(source, name)
        if literals:
            offenders[name] = literals
    assert offenders == {}, (
        "в scheduler.py снова рукописная классификация отказа морды — текст "
        "ошибки сравнивается со строковым литералом прямо в теле функции, "
        f"ходящей в морду (место правды — scripts/lib/morde_outcome.py, #1433): {offenders}"
    )


def test_the_guard_sees_a_marker_it_has_never_heard_of():
    """Главный тест этой пары, и он про саму гвардию, а не про scheduler.py.

    Прежняя редакция зашивала список известных маркеров — и пропускала
    ЧЕТВЁРТУЮ копию с новым маркером, то есть ровно тот способ, которым
    родились все три исторические (находка ai-review PR #1434). Здесь
    проверяется, что признак бесмаркерный: литерал придуман только что и ни
    в одном списке репозитория не значится."""
    source = (
        "def _archive_with_opener(repo, opener, task_numbers):\n"
        "    try:\n"
        "        pass\n"
        "    except RuntimeError as error:\n"
        '        if "СовершенноНовыйОтказКоторогоНиктоНеВидел" in str(error):\n'
        "            return []\n"
    )
    assert _string_comparisons_against_error(source, "_archive_with_opener") == [
        "СовершенноНовыйОтказКоторогоНиктоНеВидел"
    ]


def test_the_guard_does_not_fire_on_calls_that_are_not_about_the_error():
    """Обратная сторона: узость области — не формальность. Сравнение строки
    с чем-то, что не является текстом отказа, копией правила не является, и
    ложное срабатывание здесь стоило бы дороже пропуска: гвардию, которая
    кричит не по делу, начинают глушить."""
    source = (
        "def _archive_with_opener(repo, opener, task_numbers):\n"
        '    if "harness-" in session_id:\n'
        "        return []\n"
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
