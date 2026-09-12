#!/usr/bin/env python3
"""Гвардия #945: деплой обязан доказывать, что плагин runner-bridge реально
работает в чате морды, а не только что RPC-поверхность жива.

Класс проблемы: канарейки ingest-шва (#119) и реестра провайдеров (#378)
доказывают, что RPC dsh-edge отвечает — это НЕ то же самое, что «зарегистри-
рованный плагин отрабатывает при реальном вызове» (issue #100 — молчаливый
provал inject-контракта cordis один раз уже давал именно такой разрыв:
сборка зелёная, тулов нет). Прямого детерминированного RPC «вызови тул X»
у dsh-edge нет (полный список методов UNARY_ROUTES —
docs/research/12-dsh-edge-session-api.md, namespace tools.* отсутствует);
единственный путь — session.prompt (реальный ход, модель решает вызвать тул).

Правила:

  1. Шаг «Канарейка runner-bridge» существует в deploy-dsh-edge.yml.
  2. Канарейка идёт через session.prompt и просит модель вызвать runner_status.
  3. Канарейка НЕ просит модель вызвать runner_task автоматически: запись
     создаёт реальную GitHub issue с меткой task и диспетчит worker.yml —
     на каждый прогон деплоя (push + суточный schedule + ручной dispatch)
     это заспамило бы пул задач фиктивными «канарейками» и жгло минуты
     воркеров (решение #945, задокументировано в самом workflow).
  4. Целевая issue (#2) читается ЖИВЬЁМ у GitHub REST в момент канарейки
     (не хардкод ожидаемой строки) — issue #2 постоянно закрыта правилом
     репозитория «закрытая задача не переоткрывается никогда»
     (scripts/orchestra/scheduler.py::reject_reopened_tasks), стабильная
     цель без завода новой canary-задачи под каждый прогон.
  5. Канарейка падает громко, если за отведённое время не нашла реальный
     ответ инструмента.
  6. Финальный алерт называет причину отсутствия улики фактом (был ли ход,
     был ли tool/call runner_status), не списком гипотез «либо/либо»
     (класс «алерт не гадает», #472, находка ревью PR #952 п.3).
  7. Сравнение ожидаемого фрагмента идёт по текстовым значениям, извлечённым
     jq из истории, не по сырому JSON (находка ревью PR #952 п.4).
  8. Сырое событие читается через `(.event // .)` — документированная форма
     session.history кладёт его на `events[].event`, а не на сам элемент
     (находка ревью PR #952, блокирующая п.3).

Запуск: python -m pytest scripts/lib/test_deploy_runner_bridge_canary_guard.py -q
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / ".github" / "workflows" / "deploy-dsh-edge.yml"


def _text() -> str:
    assert DEPLOY.exists(), (
        f"{DEPLOY} исчез или переименован — обнови путь в гвардии "
        "сознательной правкой, а не молчаливым обходом"
    )
    return DEPLOY.read_text(encoding="utf-8")


def test_runner_bridge_canary_step_present():
    """Правило 1."""
    text = _text()
    assert "Канарейка runner-bridge" in text, (
        "деплой обязан нести живую канарейку runner-bridge (#390/#945): "
        "канарейки #119/#378 доказывают только «морда жива», не что "
        "зарегистрированный плагин реально отрабатывает в чате"
    )


def test_canary_invokes_runner_status_via_real_chat_turn():
    """Правило 2."""
    text = _text()
    assert "session.prompt" in text, (
        "канарейка runner-bridge обязана идти через session.prompt — "
        "единственный путь вызова тула (LLM во время реального хода), "
        "прямого RPC «вызови тул» у dsh-edge нет "
        "(docs/research/12-dsh-edge-session-api.md)"
    )
    assert "Вызови сейчас инструмент runner_status" in text, (
        "канарейка обязана просить модель вызвать именно runner_status"
    )


def test_canary_does_not_prompt_runner_task_automatically():
    """Правило 3."""
    text = _text()
    assert "инструмент runner_task" not in text, (
        "канарейка НЕ должна просить модель вызвать runner_task "
        "автоматически на каждом деплое — запись создаёт реальную GitHub "
        "issue с меткой task и диспетчит worker.yml (спам пула задач + "
        "минуты воркеров, решение #945)"
    )


def test_canary_targets_permanently_closed_issue_dynamically():
    """Правило 4."""
    text = _text()
    assert re.search(r"repos/\$\{GITHUB_REPOSITORY\}/issues/2[\"']", text), (
        "канарейка обязана читать заголовок issue #2 ЖИВЬЁМ у GitHub REST "
        "(эталон постоянно закрытой задачи), не хардкодить ожидаемую строку"
    )


def _missing_evidence_branch(text: str) -> str:
    """Тело `if [ -z "$found" ]; then … fi` — блок, исполняемый ТОЛЬКО когда
    канарейка не нашла улику за отведённое время (общий носитель правил 5 и 6:
    без него незачем проверять, что внутри)."""
    assert 'if [ -z "$found" ]; then' in text, (
        "канарейка обязана явной веткой проверять отсутствие улики "
        "(found пуст) — не проходить дальше тихо"
    )
    after = text.split('if [ -z "$found" ]; then', 1)[1]
    return after.split('\n          fi\n', 1)[0]


def test_canary_fails_loud_on_missing_evidence():
    """Правило 5."""
    branch = _missing_evidence_branch(_text())
    assert "::error::" in branch and "exit 1" in branch, (
        "канарейка обязана падать громко (::error:: + exit 1), если "
        "runner_status не отчитался реальным ответом за отведённое время"
    )


def test_canary_diagnoses_missing_evidence_by_fact_not_by_guessing():
    """Правило 6 (класс «алерт не гадает», #472, находка ревью PR #952, п.3):
    причина отсутствия улики называется фактом (assistant/message и tool/call
    runner_status уже прочитаны в history), не списком гипотез «либо A, либо
    B» в тексте финального алерта."""
    branch = _missing_evidence_branch(_text())
    assert 'type == "assistant/message"' in branch, (
        "алерт обязан различать «модель не сделала ни одного хода» фактом "
        "наличия assistant/message в history, не гадать"
    )
    assert 'type == "tool/call"' in branch and "runner_status" in branch, (
        "алерт обязан различать «ход был, тул не позван» фактом наличия "
        "tool/call с именем runner_status в history"
    )
    assert "либо" not in branch.lower(), (
        "финальный алерт канарейки не должен подсовывать гадание «либо A, "
        "либо B» вместо установленной причины"
    )


def test_canary_reads_history_events_via_documented_shape():
    """Правило 8 (находка ревью PR #952, блокирующая п.3): session.history
    отдаёт `{events:[{event, view?}]}` — сырое событие лежит на
    `events[].event` (docs/research/12-dsh-edge-session-api.md). Чтение
    `.type` ПРЯМО на элементах `events[]` ложно всегда: различающие причины
    (assistant/message, tool/call runner_status) не находятся НИКОГДА, и
    любой реальный отказ алерт называет «модель не сделала ни одного хода».
    `(.event // .)` читает документированную форму и не ломается на плоской.
    """
    branch = _missing_evidence_branch(_text())
    for typ in ("assistant/message", "tool/call"):
        assert re.search(
            r"\| \(\.event // \.\) \| select\(\.type == \"" + re.escape(typ), branch
        ), (
            f"диагностика канарейки обязана читать событие {typ} через "
            "(.event // .) по документированной форме session.history "
            "({events:[{event, view?}]}, research/12) — select(.type == …) "
            "прямо на элементах events[] ложно всегда и валит различение "
            "причин (проверка идёт по jq-строкам, не подсчётом по блоку: "
            "комментарий с той же подстрокой не должен красить гвардию "
            "ложно-зелёной, класс #891/#893)"
        )


def test_canary_compares_history_by_extracted_text_not_raw_json():
    """Правило 7 (находка ревью PR #952, п.4): сравнение ожидаемого
    фрагмента идёт по текстовым значениям, извлечённым jq, а не по сырому
    JSON истории — кавычка/бэкслеш в заголовке issue #2 экранируются в сырой
    JSON-строке и никогда не совпали бы с `$expect` через `grep -F` по
    сырому JSON, тихо и бессрочно."""
    text = _text()
    assert re.search(r"jq -r '\[\.\. \| strings\] \| \.\[\]'", text), (
        "канарейка обязана разворачивать текстовые значения истории через "
        "jq ('[.. | strings]'), не сравнивать expect с сырым JSON"
    )
    assert 'grep -qF "$expect" <<<"$texts"' in text, (
        "сравнение ожидаемого фрагмента обязано идти по извлечённым jq "
        "текстам ($texts), не по сырой переменной $history"
    )


# Мутации, которыми доказана гвардия (каждая — красный тест, откат — зелёный):
#
#   М1 (правило 1): переименовать шаг «Канарейка runner-bridge (#390/#945)»
#     во что-то другое — красен test_runner_bridge_canary_step_present.
#   М2 (правило 2): заменить промпт модели на «... вызвать runner_task ...»
#     — красен test_canary_invokes_runner_status_via_real_chat_turn.
#   М3 (правило 3): дописать в промпт «...а затем runner_task...» (буквально
#     «инструмент runner_task») — красен
#     test_canary_does_not_prompt_runner_task_automatically.
#   М4 (правило 4): заменить `issues/2` на хардкод-номер в другом месте
#     (issues/1) — красен test_canary_targets_permanently_closed_issue_dynamically.
#   М5 (правило 5): убрать `exit 1` из ветки `if [ -z "$found" ]; then` —
#     красен test_canary_fails_loud_on_missing_evidence.
#   М6 (правило 6): заменить причину на «либо модель не ответила, либо тул
#     не вызван» — красен test_canary_diagnoses_missing_evidence_by_fact_not_by_guessing.
#   М7 (правило 7): сравнить `$expect` с `$history` напрямую вместо
#     `$texts` — красен test_canary_compares_history_by_extracted_text_not_raw_json.
#   М8 (правило 8): убрать `(.event // .)` из одного из двух jq различения
#     (вернуть select(.type == …) прямо на элементах events[]) — красен
#     test_canary_reads_history_events_via_documented_shape.
