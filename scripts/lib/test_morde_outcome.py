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

import importlib.util
import re
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

# Рукописная классификация отказа морды — строковый маркер отказа внутри
# условия. Паттерн собран конкатенацией, чтобы гвардия не совпала сама с
# собой (живой случай: тест ловил собственный исходник, #1406).
HANDWRITTEN_RE = re.compile(
    r'if\s+"(?:' + "|".join([
        "HTTP 404",
        "session-not-" + "found",
        "SessionFormat" + "Unsupported",
        "Exceeded allowed rows",
    ]) + r')[^"]*"\s+in\s+str\('
)


def test_scheduler_has_no_handwritten_outcome_classification():
    """Место правды одно. Новая копия правила — отложенный рецидив: она будет
    знать про свой отказ и не знать про остальные, ровно как три прежние."""
    offenders = HANDWRITTEN_RE.findall(SCHEDULER.read_text(encoding="utf-8"))
    assert offenders == [], (
        "в scheduler.py снова рукописная классификация отказа морды "
        f"(место правды — scripts/lib/morde_outcome.py, #1433): {offenders}"
    )


def test_scheduler_actually_uses_the_shared_classifier():
    """Обратная сторона: гвардия выше зеленеет и тогда, когда классификации
    не осталось ВООБЩЕ — например если вызывающий просто перестал различать
    исходы. Значит нужна и проверка, что общий классификатор реально зовут."""
    text = SCHEDULER.read_text(encoding="utf-8")
    assert "morde_outcome.is_terminal(" in text, (
        "scheduler.py не зовёт общий классификатор — различение исходов пропало"
    )
    assert text.count("morde_outcome.is_terminal(") >= 2, (
        "общий классификатор зовут меньше чем из двух мест: архив и заметки — "
        "оба обязаны идти через него"
    )
